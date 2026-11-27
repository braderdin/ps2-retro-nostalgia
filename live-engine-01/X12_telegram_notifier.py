#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - MODUL NOTIFIKASI & LAPORAN TELEGRAM (X12)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X12_telegram_notifier.py
#
# FUNGSI UTAMA:
# 1. Mengimport pusat konfigurasi kunci dari X10_telegram_config.py (menyokong
#    .env.local di WSL dan TELEGRAM_ALL_KEY_JSON di GitHub Actions).
# 2. Menghantar laporan penerbitan dalam format 2 mesej HTML yang kemas:
#    - Mesej 1: Status, Timestamp MYT, Platform, Pautan FB, Pautan Shopee,
#               & Metrik Prestasi AI / Vector.
#    - Mesej 2: Teks penuh kapsyen hantaran utama & teks penuh komen pertama.
#    (Jika teks terlalu panjang melebihi had Telegram, dipecah automatik).
# 3. Menghantar laporan amaran ralat apabila berlaku kegagalan di GitHub Actions
#    atau skrip tempatan (lengkap dengan pautan terus ke log larian CI).
# 4. Sedia digunakan semula oleh semua workflow masa hadapan (Photo, Reels, IG)
#    dengan sokongan penghalaan multi-channel (`--channel`).
# ==============================================================================

import argparse
import html
import importlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ------------------------------------------------------------------------------
# IMPORT KONFIGURASI TELEGRAM BERPUSAT (X10_telegram_config.py)
# ------------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for p in [CURRENT_DIR, PROJECT_ROOT]:
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

_tg_cfg_mod: Any = importlib.import_module("X10_telegram_config")
tg_config: Any = getattr(_tg_cfg_mod, "tg_config")

MYT = timezone(timedelta(hours=8))
TELEGRAM_SAFE_TEXT_LIMIT = 3850  # Di bawah had keras 4,096 aksara Telegram Bot API


def format_myt_display(iso_str: str = "") -> str:
    """Menukar string ISO kepada paparan waktu Malaysia (MYT) yang kemas."""
    days_bm = ["Isnin", "Selasa", "Rabu", "Khamis", "Jumaat", "Sabtu", "Ahad"]
    months_bm = [
        "Jan", "Feb", "Mac", "Apr", "Mei", "Jun",
        "Jul", "Ogos", "Sep", "Okt", "Nov", "Dis",
    ]
    dt: datetime
    if iso_str:
        try:
            dt = datetime.fromisoformat(iso_str).astimezone(MYT)
        except Exception:
            dt = datetime.now(tz=MYT)
    else:
        dt = datetime.now(tz=MYT)

    return (
        f"{days_bm[dt.weekday()]}, {dt.day:02d} {months_bm[dt.month - 1]} {dt.year} "
        f"| {dt.strftime('%I:%M:%S %p')} MYT"
    )


def build_facebook_post_url(fb_post_id: str) -> str:
    """Membina pautan terus ke hantaran Facebook daripada format {page_id}_{story_id}."""
    clean_id = (fb_post_id or "").strip()
    if not clean_id or clean_id.upper() == "DRY_RUN":
        return ""
    if "_" in clean_id:
        page_part, post_part = clean_id.split("_", 1)
        return f"https://www.facebook.com/{page_part}/posts/{post_part}"
    return f"https://www.facebook.com/{clean_id}"


def sanitize_sensitive_tokens(raw_text: str) -> str:
    """Menopengkan sebarang token rahsia sebelum dihantar ke mesej ralat Telegram."""
    if not raw_text:
        return ""
    cleaned = raw_text
    secret_candidates = [
        getattr(tg_config, "bot_token", ""),
        getattr(tg_config, "api_hash", ""),
        os.getenv("PS2N_FB_PAGE_TOKEN", ""),
        os.getenv("PS2N_FB_APP_SECRET", ""),
        os.getenv("PS2N_PROXY_SECRET", ""),
        os.getenv("PS2N_UNSPLASH_ACCESS_KEY", ""),
        os.getenv("PS2N_PEXELS_API_KEY", ""),
    ]
    for sec in secret_candidates:
        if sec and len(sec) >= 8 and sec in cleaned:
            cleaned = cleaned.replace(sec, f"{sec[:4]}***REDACTED***")
    return cleaned


def split_html_safe_chunks(text: str, max_len: int = TELEGRAM_SAFE_TEXT_LIMIT) -> List[str]:
    """Memecahkan teks panjang kepada beberapa bahagian supaya tidak melebihi had Telegram."""
    if len(text) <= max_len:
        return [text]

    chunks: List[str] = []
    remaining = text
    while len(remaining) > max_len:
        cut = remaining.rfind("\n", 0, max_len)
        if cut < int(max_len * 0.5):
            cut = remaining.rfind(" ", 0, max_len)
        if cut <= 0:
            cut = max_len
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def get_github_actions_context() -> Dict[str, str]:
    """Mengumpul metadata larian GitHub Actions jika dijalankan di pelayan CI."""
    is_ci = os.getenv("GITHUB_ACTIONS", "").lower() == "true"
    if not is_ci:
        return {
            "is_ci": "false",
            "env_label": "LOCAL_WSL",
            "workflow": "Manual Local Execution",
            "run_number": "-",
            "run_url": "",
            "actor": os.getenv("USER", "braderdin"),
            "ref_name": "local",
        }

    server_url = os.getenv("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    repo = os.getenv("GITHUB_REPOSITORY", "braderdin/ps2-retro-nostalgia")
    run_id = os.getenv("GITHUB_RUN_ID", "")
    run_url = f"{server_url}/{repo}/actions/runs/{run_id}" if run_id else ""

    return {
        "is_ci": "true",
        "env_label": "GITHUB_ACTIONS",
        "workflow": os.getenv("GITHUB_WORKFLOW", "GitHub Workflow"),
        "run_number": os.getenv("GITHUB_RUN_NUMBER", "-"),
        "run_url": run_url,
        "actor": os.getenv("GITHUB_ACTOR", "github-actions"),
        "ref_name": os.getenv("GITHUB_REF_NAME", "main"),
    }


# ==============================================================================
# KELAS UTAMA PENGHANTAR NOTIFIKASI TELEGRAM (X12)
# ==============================================================================
class TelegramNotifier:
    """
    Enjin penghantaran laporan & amaran ralat Telegram berasaskan X10_telegram_config.
    Menyokong:
      - Penghantaran berbilang mesej (Mesej 1: Ringkasan & Link, Mesej 2: Ayat Penuh)
      - Penghantaran gambar (`send_photo`) untuk workflow bergambar masa hadapan
      - Penghalaan multi-channel (`channel_alias`) + salinan DM Admin (`also_dm`)
    """

    def __init__(self) -> None:
        self.cfg = tg_config
        self.timeout = 25.0

    def _ensure_ready(self) -> None:
        ok, errs = self.cfg.validate()
        if not ok:
            raise RuntimeError(f"Konfigurasi Telegram X10 tidak lengkap: {'; '.join(errs)}")

    def send_message(
        self,
        html_text: str,
        chat_id: Optional[str] = None,
        channel_alias: str = "default",
        disable_preview: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Menghantar mesej HTML ke sasaran Telegram dan memecahkan kepada beberapa
        mesej secara automatik sekiranya melebihi had aksara.
        """
        self._ensure_ready()
        target_id = (chat_id or self.cfg.get_channel_id(channel_alias)).strip()
        if not target_id:
            raise RuntimeError(f"Chat ID Telegram tidak ditemui untuk alias '{channel_alias}'.")

        url = f"{self.cfg.bot_api_base_url}/sendMessage"
        chunks = split_html_safe_chunks(html_text, max_len=TELEGRAM_SAFE_TEXT_LIMIT)
        sent_results: List[Dict[str, Any]] = []

        for idx, chunk in enumerate(chunks, 1):
            t0 = time.perf_counter()
            resp = requests.post(
                url,
                json={
                    "chat_id": target_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": disable_preview,
                },
                timeout=self.timeout,
            )
            latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            data = resp.json()

            if resp.status_code != 200 or not data.get("ok"):
                raise RuntimeError(
                    f"Gagal hantar mesej Telegram (bahagian {idx}/{len(chunks)}) ke {target_id}: {json.dumps(data)}"
                )

            sent_results.append(
                {
                    "part": idx,
                    "total_parts": len(chunks),
                    "chat_id": target_id,
                    "message_id": data["result"]["message_id"],
                    "latency_ms": latency_ms,
                }
            )
            if idx < len(chunks):
                time.sleep(0.6)

        return sent_results

    def send_photo(
        self,
        image_path: Path,
        html_caption: str = "",
        chat_id: Optional[str] = None,
        channel_alias: str = "default",
    ) -> Dict[str, Any]:
        """Menghantar gambar berserta kapsyen HTML (sedia untuk workflow bergambar masa hadapan)."""
        self._ensure_ready()
        target_id = (chat_id or self.cfg.get_channel_id(channel_alias)).strip()
        if not image_path.exists():
            raise FileNotFoundError(f"Fail gambar tidak ditemui: {image_path}")

        url = f"{self.cfg.bot_api_base_url}/sendPhoto"
        safe_caption = html_caption[:1000]
        t0 = time.perf_counter()

        with open(image_path, "rb") as f:
            resp = requests.post(
                url,
                data={
                    "chat_id": target_id,
                    "caption": safe_caption,
                    "parse_mode": "HTML",
                },
                files={"photo": (image_path.name, f, "image/jpeg")},
                timeout=35.0,
            )
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        data = resp.json()
        if resp.status_code != 200 or not data.get("ok"):
            raise RuntimeError(f"Gagal hantar gambar Telegram ke {target_id}: {json.dumps(data)}")

        return {
            "chat_id": target_id,
            "message_id": data["result"]["message_id"],
            "latency_ms": latency_ms,
        }

    # --------------------------------------------------------------------------
    # PEMBINA 2 MESEJ LAPORAN KEJAYAAN POST (MESEJ 1: RINGKASAN | MESEJ 2: AYAT PENUH)
    # --------------------------------------------------------------------------
    def build_post_success_messages(
        self,
        report_data: Dict[str, Any],
        platform_label: str = "Facebook Page (Text-Only)",
    ) -> Tuple[str, str]:
        """
        Membina pasangan 2 mesej cantik & teratur:
        - Mesej 1: Ringkasan eksekutif, Timestamp MYT, Pautan FB, Pautan Shopee, & Metrik.
        - Mesej 2: Contoh ayat penuh yang dipost ke FB & ayat komen pertama.
        """
        ci = get_github_actions_context()
        posted_iso = str(report_data.get("posted_at_iso", ""))
        myt_display = format_myt_display(posted_iso)

        fb_post_id = str(report_data.get("fb_post_id", "")).strip()
        fb_comment_id = str(report_data.get("fb_comment_id", "")).strip()
        is_dry_run = fb_post_id.upper() == "DRY_RUN" or not fb_post_id
        fb_url = build_facebook_post_url(fb_post_id)

        persona_ref = str(report_data.get("persona_ref", "admin")).strip()
        pillar_id = str(report_data.get("pillar_id", "-")).strip()
        game_title = str(report_data.get("game_title", "-")).strip()
        story_chars = int(report_data.get("story_chars", len(str(report_data.get("story_bm", "")))))

        shopee_code = str(report_data.get("shopee_product_code", "-")).strip()
        shopee_name = str(report_data.get("shopee_product_name", "-")).strip()
        shopee_url = str(report_data.get("shopee_url", "-")).strip()
        var_idx = report_data.get("comment_variant_idx")
        var_display = f"Variasi #{int(var_idx) + 1}" if var_idx is not None else "-"

        metrics = report_data.get("llm_metrics", {}) or {}
        elapsed_sec = metrics.get("elapsed_sec", "-")
        tok_sec = metrics.get("tok_per_sec", "-")
        attempt_num = metrics.get("attempt", 1)
        vec_shard = report_data.get("vector_shard", "-")

        status_header = (
            "🧪 <b>LAPORAN UJIAN DRY-RUN :: PS2 RETRO NOSTALGIA</b>"
            if is_dry_run
            else "✅ <b>LAPORAN SIARAN BERJAYA :: PS2 RETRO NOSTALGIA</b>"
        )

        # --- MESEJ 1: RINGKASAN OPERASI, TIMESTAMP & PAUTAN ---
        msg1_lines = [
            status_header,
            f"📡 <b>Platform:</b> {html.escape(platform_label)}",
            f"🕒 <b>Masa Siaran:</b> {html.escape(myt_display)}",
            f"⚙️ <b>Persekitaran:</b> <code>{html.escape(ci['env_label'])}</code>"
            + (f" (Run #{html.escape(ci['run_number'])})" if ci["is_ci"] == "true" else ""),
            "",
            "🎮 <b>BUTIRAN KANDUNGAN &amp; PERSONA</b>",
            f"• <b>Persona AI:</b> <code>{html.escape(persona_ref)}</code> (Pilar: <code>{html.escape(pillar_id)}</code>)",
            f"• <b>Rujukan Game:</b> {html.escape(game_title)}",
            f"• <b>Panjang Cerita:</b> {story_chars} aksara (Sifar Emoji)",
            "",
            "🔗 <b>PAUTAN SIARAN &amp; PRODUK SHOPEE</b>",
        ]

        if fb_url:
            msg1_lines.append(f"• <b>Pautan Post FB:</b> {html.escape(fb_url)}")
            msg1_lines.append(f"• <b>FB Post ID:</b> <code>{html.escape(fb_post_id)}</code>")
        else:
            msg1_lines.append(f"• <b>Status FB:</b> <code>{html.escape(fb_post_id or 'TIADA')}</code>")

        if fb_comment_id and fb_comment_id.upper() != "DRY_RUN":
            msg1_lines.append(f"• <b>FB Comment ID:</b> <code>{html.escape(fb_comment_id)}</code>")

        msg1_lines.extend(
            [
                f"• <b>Produk Shopee:</b> <code>{html.escape(shopee_code)}</code> — {html.escape(shopee_name[:75])}",
                f"• <b>Link Shopee Digunakan:</b> {html.escape(shopee_url)}",
                f"• <b>Templat Komen:</b> {html.escape(var_display)}",
                "",
                "📊 <b>METRIK ENJIN AI &amp; CLOUD</b>",
                f"• <b>Janaan LLM:</b> {elapsed_sec}s ({tok_sec} tok/s | Cubaan #{attempt_num})",
                f"• <b>Semantik Vector:</b> Shard #{vec_shard} (Lulus Unik)",
            ]
        )

        if ci["run_url"]:
            msg1_lines.append(f"• <b>Log GitHub Actions:</b> {html.escape(ci['run_url'])}")

        msg1_lines.append("\n<i>👇 Contoh ayat penuh hantaran &amp; komen pertama dihantar pada mesej berikutnya.</i>")
        message_1 = "\n".join(msg1_lines)

        # --- MESEJ 2: CONTOH AYAT PENUH POST & KOMEN PERTAMA ---
        full_caption = str(report_data.get("post_caption") or report_data.get("story_bm") or "").strip()
        full_comment = str(report_data.get("comment_text") or "").strip()

        msg2_lines = [
            "📝 <b>CONTOH AYAT HANTARAN UTAMA (FACEBOOK)</b>",
            f"🕒 <i>{html.escape(myt_display)}</i>",
            "━━━━━━━━━━━━━━━━━━━━",
            html.escape(full_caption),
            "━━━━━━━━━━━━━━━━━━━━",
            "",
            f"💬 <b>KOMEN PERTAMA AUTOMATIK ({html.escape(shopee_code)})</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            html.escape(full_comment),
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        message_2 = "\n".join(msg2_lines)

        return message_1, message_2

    # --------------------------------------------------------------------------
    # PEMBINA MESEJ LAPORAN RALAT (GITHUB ACTIONS / LOKAL)
    # --------------------------------------------------------------------------
    def build_error_alert_messages(
        self,
        platform_label: str = "Facebook Page (Text-Only)",
        failed_step: str = "Unknown Step",
        error_detail: str = "",
        log_excerpt: str = "",
    ) -> List[str]:
        """Membina mesej amaran ralat lengkap bersama pautan log GitHub Actions."""
        ci = get_github_actions_context()
        myt_display = format_myt_display()

        safe_step = html.escape(sanitize_sensitive_tokens(failed_step or "Unknown Step"))
        safe_detail = html.escape(sanitize_sensitive_tokens(error_detail or "Skrip berhenti dengan kod ralat bukan sifar."))
        safe_excerpt = html.escape(sanitize_sensitive_tokens(log_excerpt.strip()))

        lines = [
            "🚨 <b>AMARAN RALAT SISTEM :: PS2 RETRO NOSTALGIA</b>",
            f"📡 <b>Platform / Modul:</b> {html.escape(platform_label)}",
            f"🕒 <b>Masa Kejadian:</b> {html.escape(myt_display)}",
            f"⚙️ <b>Persekitaran:</b> <code>{html.escape(ci['env_label'])}</code>"
            + (f" (Workflow: <b>{html.escape(ci['workflow'])}</b> | Run #{html.escape(ci['run_number'])})" if ci["is_ci"] == "true" else ""),
            "",
            "❌ <b>BUTIRAN KEGAGALAN</b>",
            f"• <b>Langkah Gagal:</b> <code>{safe_step}</code>",
            f"• <b>Punca / Status:</b> {safe_detail}",
        ]

        if ci["run_url"]:
            lines.append(f"• <b>Semak Log Penuh CI:</b> {html.escape(ci['run_url'])}")

        if safe_excerpt:
            # Hadkan sedutan log supaya muat dengan cantik dalam Telegram
            trimmed_log = safe_excerpt[-1800:] if len(safe_excerpt) > 1800 else safe_excerpt
            lines.extend(
                [
                    "",
                    "🧾 <b>SEDUTAN LOG TERAKHIR</b>",
                    f"<pre>{trimmed_log}</pre>",
                ]
            )

        lines.append("\n⚠️ <i>Sila semak log di atas untuk tindakan pembetulan.</i>")
        full_alert = "\n".join(lines)
        return split_html_safe_chunks(full_alert, max_len=TELEGRAM_SAFE_TEXT_LIMIT)

    # --------------------------------------------------------------------------
    # PENGENDALI BERSEPADU: HANTAR LAPORAN KEJAYAAN ATAU RALAT
    # --------------------------------------------------------------------------
    def notify_post_success(
        self,
        report_data: Dict[str, Any],
        platform_label: str = "Facebook Page (Text-Only)",
        channel_alias: str = "default",
        also_dm: bool = False,
    ) -> Dict[str, Any]:
        """Menghantar 2 mesej laporan kejayaan ke Channel Telegram (dan DM jika diminta)."""
        msg1, msg2 = self.build_post_success_messages(report_data=report_data, platform_label=platform_label)

        res_m1 = self.send_message(msg1, channel_alias=channel_alias, disable_preview=True)
        time.sleep(0.8)
        res_m2 = self.send_message(msg2, channel_alias=channel_alias, disable_preview=True)

        dm_res: List[Dict[str, Any]] = []
        if also_dm and self.cfg.admin_chat_id:
            dm_res.extend(self.send_message(msg1, chat_id=self.cfg.admin_chat_id, disable_preview=True))
            time.sleep(0.6)
            dm_res.extend(self.send_message(msg2, chat_id=self.cfg.admin_chat_id, disable_preview=True))

        return {
            "mode": "SUCCESS_2_MESSAGES",
            "channel_alias": channel_alias,
            "channel_id": self.cfg.get_channel_id(channel_alias),
            "message_1_parts": res_m1,
            "message_2_parts": res_m2,
            "dm_parts": dm_res,
        }

    def notify_workflow_error(
        self,
        platform_label: str = "Facebook Page (Text-Only)",
        failed_step: str = "Unknown Step",
        error_detail: str = "",
        log_excerpt: str = "",
        channel_alias: str = "default",
        also_dm: bool = True,
    ) -> Dict[str, Any]:
        """Menghantar laporan amaran ralat ke Channel Telegram dan DM Admin."""
        alert_chunks = self.build_error_alert_messages(
            platform_label=platform_label,
            failed_step=failed_step,
            error_detail=error_detail,
            log_excerpt=log_excerpt,
        )

        ch_sent: List[Dict[str, Any]] = []
        for chunk in alert_chunks:
            ch_sent.extend(self.send_message(chunk, channel_alias=channel_alias, disable_preview=True))

        dm_sent: List[Dict[str, Any]] = []
        if also_dm and self.cfg.admin_chat_id:
            for chunk in alert_chunks:
                dm_sent.extend(self.send_message(chunk, chat_id=self.cfg.admin_chat_id, disable_preview=True))

        return {
            "mode": "ERROR_ALERT",
            "channel_alias": channel_alias,
            "channel_id": self.cfg.get_channel_id(channel_alias),
            "channel_parts": ch_sent,
            "dm_parts": dm_sent,
        }


# Singleton Instance sedia diimport oleh mana-mana skrip lain
tg_notifier = TelegramNotifier()


def main() -> None:
    parser = argparse.ArgumentParser(description="X12: Modul Notifikasi & Laporan Telegram Multi-Channel")
    parser.add_argument(
        "--status",
        type=str,
        default="success",
        choices=["success", "failure"],
        help="Status pelaksanaan workflow ('success' atau 'failure').",
    )
    parser.add_argument(
        "--report-json",
        type=str,
        default=str(PROJECT_ROOT / "temp" / "x11_last_text_post.json"),
        help="Laluan fail JSON laporan hantaran (contoh: temp/x11_last_text_post.json).",
    )
    parser.add_argument(
        "--platform",
        type=str,
        default="Facebook Page (Text-Only)",
        help="Label platform/modul untuk paparan laporan.",
    )
    parser.add_argument(
        "--channel",
        type=str,
        default="default",
        help="Alias channel dalam X10_telegram_config (contoh: 'default', 'fb', 'error').",
    )
    parser.add_argument(
        "--also-dm",
        action="store_true",
        help="Hantar juga salinan mesej ke DM peribadi Admin.",
    )
    parser.add_argument(
        "--error-step",
        type=str,
        default="",
        help="Nama step yang gagal (digunakan apabila --status failure).",
    )
    parser.add_argument(
        "--error-detail",
        type=str,
        default="",
        help="Keterangan ringkas punca kegagalan.",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default="",
        help="Laluan fail log terminal untuk diselitkan sedutan ralat terakhir.",
    )
    args = parser.parse_args()

    console.print(
        Panel.fit(
            "[bold cyan]PS2-RETRO-NOSTALGIA :: TELEGRAM NOTIFIER & ERROR REPORTER (X12)[/bold cyan]\n"
            f"[dim]Sumber Kunci X10: {tg_config.source_mode} | Channel Sasaran: '{args.channel}' "
            f"({tg_config.get_channel_id(args.channel)})[/dim]",
            border_style="cyan",
        )
    )

    report_path = Path(args.report_json)
    report_data: Dict[str, Any] = {}
    if report_path.exists():
        try:
            report_data = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as exc:
            report_data = {"_json_parse_error": str(exc)}

    log_excerpt = ""
    if args.log_file and Path(args.log_file).exists():
        try:
            lines = Path(args.log_file).read_text(encoding="utf-8", errors="ignore").splitlines()
            log_excerpt = "\n".join(lines[-35:])
        except Exception:
            pass

    if args.status == "success" and report_data and not report_data.get("_json_parse_error"):
        res = tg_notifier.notify_post_success(
            report_data=report_data,
            platform_label=args.platform,
            channel_alias=args.channel,
            also_dm=args.also_dm,
        )
        tbl = Table(title="Laporan Kejayaan Dihantar ke Telegram (2 Mesej)", box=box.ROUNDED, header_style="bold green")
        tbl.add_column("Bahagian", style="bold white")
        tbl.add_column("Channel ID", style="cyan")
        tbl.add_column("Message ID", justify="center", style="yellow")
        tbl.add_column("Latensi", justify="right", style="dim")

        for p in res["message_1_parts"]:
            tbl.add_row(f"Mesej 1 (Ringkasan & Link) [{p['part']}/{p['total_parts']}]", p["chat_id"], str(p["message_id"]), f"{p['latency_ms']} ms")
        for p in res["message_2_parts"]:
            tbl.add_row(f"Mesej 2 (Ayat Penuh & Komen) [{p['part']}/{p['total_parts']}]", p["chat_id"], str(p["message_id"]), f"{p['latency_ms']} ms")
        console.print(tbl)
    else:
        # Mod ralat (sama ada --status failure atau fail laporan JSON tidak terhasil)
        err_step = args.error_step or "Penerbitan / Penjanaan Kandungan"
        err_detail = args.error_detail
        if not err_detail:
            if report_data.get("error"):
                err_detail = str(report_data["error"])
            elif not report_path.exists():
                err_detail = f"Fail laporan {report_path.name} tidak ditemui (skrip mungkin terhenti awal)."
            else:
                err_detail = "Larian workflow berakhir dengan status kegagalan."

        res = tg_notifier.notify_workflow_error(
            platform_label=args.platform,
            failed_step=err_step,
            error_detail=err_detail,
            log_excerpt=log_excerpt,
            channel_alias=args.channel,
            also_dm=True,
        )
        console.print(
            Panel(
                f"[bold red]🚨 Laporan Ralat Dihantar ke Telegram![/bold red]\n"
                f"• Channel ID: [cyan]{res['channel_id']}[/cyan] ({len(res['channel_parts'])} mesej)\n"
                f"• DM Admin: [cyan]{tg_config.admin_chat_id}[/cyan] ({len(res['dm_parts'])} mesej)",
                border_style="red",
            )
        )


if __name__ == "__main__":
    main()