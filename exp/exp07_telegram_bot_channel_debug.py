#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - DIAGNOSTIK BOT & CHANNEL TELEGRAM (EXP07)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/exp/exp07_telegram_bot_channel_debug.py
#
# FUNGSI UJIAN:
# 1. Mengaudit kesemua 14 kunci PS2N_TG_* daripada .env.local / GitHub Secrets.
# 2. Mengesahkan API Bot (getMe) & status pentadbir di Channel (getChatMember).
# 3. Menghantar mesej laporan ujian ke DM Admin (PS2N_TG_CHAT_ID) dan
#    Channel Rasmi (PS2N_TG_CHANNEL_ID).
# 4. Memilih gambar dari /temp/test_media_pool_50/raw/ dan menghantarnya ke
#    kedua-dua destinasi Telegram.
# ==============================================================================

import html
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

import requests
from dotenv import load_dotenv
from PIL import Image
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ------------------------------------------------------------------------------
# KONFIGURASI LALUAN & MUAT NAIK .env.local
# ------------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
LIVE_ENGINE_DIR = PROJECT_ROOT / "live-engine-01"

for p in [CURRENT_DIR, LIVE_ENGINE_DIR, PROJECT_ROOT]:
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

ENV_LOCAL_PATH = PROJECT_ROOT / ".env.local"
if ENV_LOCAL_PATH.exists():
    load_dotenv(dotenv_path=ENV_LOCAL_PATH, override=False)

try:
    cfg: Any = importlib.import_module("X01_config")
except Exception:
    cfg = None

RAW_POOL_DIR: Path = PROJECT_ROOT / "temp" / "test_media_pool_50" / "raw"
MYT = timezone(timedelta(hours=8))


def get_myt_timestamp() -> str:
    now = datetime.now(tz=MYT)
    days_bm = ["Isnin", "Selasa", "Rabu", "Khamis", "Jumaat", "Sabtu", "Ahad"]
    months_bm = [
        "Januari", "Februari", "Mac", "April", "Mei", "Jun",
        "Julai", "Ogos", "September", "Oktober", "November", "Disember",
    ]
    return (
        f"{days_bm[now.weekday()]}, {now.day} {months_bm[now.month - 1]} {now.year} "
        f"({now.strftime('%I:%M:%S %p')} MYT)"
    )


def mask_secret(val: str, head: int = 6, tail: int = 6) -> str:
    if not val:
        return "-"
    clean = val.strip()
    if "BEGIN RSA PUBLIC KEY" in clean:
        return f"RSA PUBLIC KEY ({len(clean)} aksara)"
    if len(clean) <= (head + tail + 2):
        return clean
    return f"{clean[:head]}...{clean[-tail:]}"


# ==============================================================================
# 1. AUDIT 14 KUNCI TELEGRAM DALAM .env.local
# ==============================================================================
def audit_telegram_env_keys() -> Tuple[bool, Dict[str, str]]:
    bot_token = os.getenv("PS2N_TG_BOT_TOKEN", "").strip()
    expected_bot_id = bot_token.split(":", 1)[0] if ":" in bot_token else ""

    specs: List[Tuple[str, bool, Any]] = [
        ("PS2N_TG_BOT_LINK", True, lambda v: v.startswith("https://t.me/")),
        ("PS2N_TG_BOT_TOKEN", True, lambda v: ":" in v and len(v) >= 35),
        ("PS2N_TG_BOT_ID", True, lambda v: v.isdigit() and (not expected_bot_id or v == expected_bot_id)),
        ("PS2N_TG_CHAT_ID", True, lambda v: v.lstrip("-").isdigit()),
        ("PS2N_TG_CHANNEL_URL", True, lambda v: v.startswith("https://t.me/")),
        ("PS2N_TG_CHANNEL_ID", True, lambda v: v.startswith("-100") and v[4:].isdigit()),
        ("PS2N_TG_APP_API_ID", True, lambda v: v.isdigit()),
        ("PS2N_TG_APP_API_HASH", True, lambda v: bool(re.fullmatch(r"[0-9a-fA-F]{32}", v))),
        ("PS2N_TG_APP_TITLE", True, lambda v: len(v) >= 2),
        ("PS2N_TG_SHORT_NAME", True, lambda v: len(v) >= 2),
        ("PS2N_TG_TEST_CONFIGURATION", True, lambda v: bool(re.fullmatch(r"\d+\.\d+\.\d+\.\d+:\d+", v))),
        ("PS2N_TG_TEST_PUBLIC_KEY", True, lambda v: "BEGIN RSA PUBLIC KEY" in v and "END RSA PUBLIC KEY" in v),
        ("PS2N_TG_PRODUCTION_CONFIGURATION", True, lambda v: bool(re.fullmatch(r"\d+\.\d+\.\d+\.\d+:\d+", v))),
        ("PS2N_TG_PRODUCTION_PUBLIC_KEYS", True, lambda v: "BEGIN RSA PUBLIC KEY" in v and "END RSA PUBLIC KEY" in v),
        ("PS2N_TG_EXTRA_CHANNELS_JSON", False, lambda v: True),
    ]

    tbl = Table(
        title="1. Audit Kunci Telegram (.env.local / Environment)",
        box=box.ROUNDED,
        header_style="bold magenta",
    )
    tbl.add_column("#", justify="center", style="cyan", width=3)
    tbl.add_column("Pemboleh Ubah", style="bold white")
    tbl.add_column("Nilai (Bertopeng)", style="cyan")
    tbl.add_column("Jenis", justify="center", style="dim")
    tbl.add_column("Status", justify="center")

    env_map: Dict[str, str] = {}
    all_required_ok = True

    for idx, (key_name, is_required, validator) in enumerate(specs, 1):
        raw_val = os.getenv(key_name, "").strip()
        env_map[key_name] = raw_val

        if not raw_val:
            if is_required:
                all_required_ok = False
                status_badge = "[bold red]TIADA (WAJIB)[/bold red]"
            else:
                status_badge = "[dim]PILIHAN (BELUM SET)[/dim]"
            masked_disp = "-"
        else:
            is_valid = False
            try:
                is_valid = bool(validator(raw_val))
            except Exception:
                is_valid = False

            if is_valid:
                status_badge = "[bold green]SAH & ADA[/bold green]"
            else:
                if is_required:
                    all_required_ok = False
                status_badge = "[bold yellow]FORMAT RAGU[/bold yellow]"

            # Papar penuh untuk nilai bukan rahsia seperti tajuk/IP/pautan
            if key_name in (
                "PS2N_TG_BOT_LINK",
                "PS2N_TG_APP_TITLE",
                "PS2N_TG_SHORT_NAME",
                "PS2N_TG_TEST_CONFIGURATION",
                "PS2N_TG_PRODUCTION_CONFIGURATION",
            ):
                masked_disp = raw_val
            else:
                masked_disp = mask_secret(raw_val)

        tbl.add_row(
            str(idx),
            key_name,
            masked_disp,
            "Wajib" if is_required else "Pilihan",
            status_badge,
        )

    console.print(tbl)
    return all_required_ok, env_map


# ==============================================================================
# 2. PENGESAHAN BOT API & HAK PENTADBIR CHANNEL
# ==============================================================================
def verify_bot_and_channel(env_map: Dict[str, str]) -> Tuple[bool, Dict[str, Any]]:
    token = env_map["PS2N_TG_BOT_TOKEN"]
    channel_id = env_map["PS2N_TG_CHANNEL_ID"]
    base_api = f"https://api.telegram.org/bot{token}"

    info: Dict[str, Any] = {}

    # A. Semak getMe
    t0 = time.perf_counter()
    r_me = requests.get(f"{base_api}/getMe", timeout=20)
    me_ms = round((time.perf_counter() - t0) * 1000, 1)
    me_data = r_me.json()
    if r_me.status_code != 200 or not me_data.get("ok"):
        return False, {"error": f"getMe gagal: {json.dumps(me_data)}"}

    bot_user = me_data["result"]
    bot_id = bot_user["id"]
    bot_username = bot_user.get("username", "")
    info["bot_id"] = bot_id
    info["bot_username"] = f"@{bot_username}"
    info["bot_name"] = bot_user.get("first_name", "")
    info["getme_ms"] = me_ms

    # B. Semak getChat untuk Channel
    t1 = time.perf_counter()
    r_chat = requests.get(f"{base_api}/getChat", params={"chat_id": channel_id}, timeout=20)
    chat_ms = round((time.perf_counter() - t1) * 1000, 1)
    chat_data = r_chat.json()
    if r_chat.status_code != 200 or not chat_data.get("ok"):
        return False, {"error": f"getChat channel gagal: {json.dumps(chat_data)}"}

    chat_res = chat_data["result"]
    info["channel_title"] = chat_res.get("title", "Channel")
    info["channel_type"] = chat_res.get("type", "channel")
    info["getchat_ms"] = chat_ms

    # C. Semak status pentadbir bot dalam Channel
    r_mem = requests.get(
        f"{base_api}/getChatMember",
        params={"chat_id": channel_id, "user_id": bot_id},
        timeout=20,
    )
    mem_data = r_mem.json()
    if r_mem.status_code != 200 or not mem_data.get("ok"):
        return False, {"error": f"getChatMember gagal: {json.dumps(mem_data)}"}

    mem_res = mem_data["result"]
    status = mem_res.get("status", "")
    can_post = mem_res.get("can_post_messages", status == "creator")
    info["member_status"] = status
    info["can_post_messages"] = bool(can_post)

    if status not in ("administrator", "creator") or not can_post:
        return False, {"error": f"Bot bukan admin yang boleh hantar mesej di channel (status={status})."}

    return True, info


# ==============================================================================
# 3. PILIH GAMBAR UJIAN DARI temp/test_media_pool_50/raw/
# ==============================================================================
def pick_sample_image() -> Path:
    RAW_POOL_DIR.mkdir(parents=True, exist_ok=True)
    imgs = sorted([p for p in RAW_POOL_DIR.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")])
    if imgs:
        return random.choice(imgs)

    # Fallback jika folder raw kosong (contohnya jika dijalankan di runner baru)
    fallback_path = RAW_POOL_DIR / "tg_fallback_test.jpg"
    im = Image.new("RGB", (640, 360), color=(18, 28, 54))
    im.save(fallback_path, format="JPEG", quality=85)
    return fallback_path


# ==============================================================================
# 4. HANTAR MESEJ TEKS & GAMBAR KE TELEGRAM (DM & CHANNEL)
# ==============================================================================
def send_telegram_text(
    bot_token: str,
    target_chat_id: str,
    html_message: str,
) -> Tuple[bool, Dict[str, Any]]:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    t0 = time.perf_counter()
    resp = requests.post(
        url,
        json={
            "chat_id": target_chat_id,
            "text": html_message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=25,
    )
    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    data = resp.json()
    if resp.status_code != 200 or not data.get("ok"):
        return False, {"latency_ms": latency_ms, "error": json.dumps(data)}
    return True, {"latency_ms": latency_ms, "message_id": data["result"]["message_id"]}


def send_telegram_photo(
    bot_token: str,
    target_chat_id: str,
    image_path: Path,
    html_caption: str,
) -> Tuple[bool, Dict[str, Any]]:
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    t0 = time.perf_counter()
    with open(image_path, "rb") as f:
        resp = requests.post(
            url,
            data={
                "chat_id": target_chat_id,
                "caption": html_caption,
                "parse_mode": "HTML",
            },
            files={"photo": (image_path.name, f, "image/jpeg")},
            timeout=35,
        )
    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    data = resp.json()
    if resp.status_code != 200 or not data.get("ok"):
        return False, {"latency_ms": latency_ms, "error": json.dumps(data)}
    return True, {"latency_ms": latency_ms, "message_id": data["result"]["message_id"]}


def main() -> None:
    myt_now = get_myt_timestamp()
    console.print(
        Panel.fit(
            "[bold cyan]PS2-RETRO-NOSTALGIA :: TELEGRAM BOT & MULTI-CHANNEL DIAGNOSTIC (EXP07)[/bold cyan]\n"
            f"[dim]Masa MYT: {myt_now} | Sumber Env: {ENV_LOCAL_PATH}[/dim]",
            border_style="cyan",
        )
    )

    # Langkah 1: Audit semua kunci PS2N_TG_*
    keys_ok, env_map = audit_telegram_env_keys()
    if not keys_ok:
        console.print("[bold red]❌ Sebahagian kunci wajib PS2N_TG_* tiada atau tidak sah dalam .env.local.[/bold red]")
        sys.exit(1)

    # Langkah 2: Sahkan Bot & Kebenaran Channel
    with console.status("[bold cyan]Mengesahkan identiti Bot & status Admin di Channel Telegram...[/bold cyan]"):
        v_ok, v_info = verify_bot_and_channel(env_map)

    if not v_ok:
        console.print(Panel(f"[bold red]Ralat Pengesahan Telegram:[/bold red] {v_info.get('error')}", border_style="red"))
        sys.exit(1)

    verify_tbl = Table(title="2. Pengesahan Sambungan Bot & Channel Telegram", box=box.ROUNDED, header_style="bold green")
    verify_tbl.add_column("Komponen", style="bold white")
    verify_tbl.add_column("Butiran", style="cyan")
    verify_tbl.add_column("Respons API", justify="right", style="yellow")
    verify_tbl.add_row("Bot Rasmi", f"{v_info['bot_name']} ({v_info['bot_username']} | ID: {v_info['bot_id']})", f"{v_info['getme_ms']} ms")
    verify_tbl.add_row("Channel Sasaran", f"{v_info['channel_title']} ({env_map['PS2N_TG_CHANNEL_ID']})", f"{v_info['getchat_ms']} ms")
    verify_tbl.add_row("Kebenaran Channel", f"Status: {v_info['member_status'].upper()} | Post Messages: {v_info['can_post_messages']}", "SAH")
    console.print(verify_tbl)

    bot_token = env_map["PS2N_TG_BOT_TOKEN"]
    dm_chat_id = env_map["PS2N_TG_CHAT_ID"]
    channel_id = env_map["PS2N_TG_CHANNEL_ID"]
    sample_img = pick_sample_image()
    img_kb = round(sample_img.stat().st_size / 1024, 1)

    # Langkah 3 & 4: Hantar Mesej Teks & Gambar ke DM Admin serta Channel
    results_rows: List[Tuple[str, str, bool, Dict[str, Any]]] = []

    text_msg_dm = (
        f"🎮 <b>PS2 RETRO NOSTALGIA — UJIAN SISTEM NOTIFIKASI (DM)</b>\n\n"
        f"✅ Sambungan Bot <code>{html.escape(v_info['bot_username'])}</code> berjaya disahkan.\n"
        f"🕒 <b>Masa MYT:</b> {html.escape(myt_now)}\n"
        f"📡 <b>Channel Terpaut:</b> {html.escape(v_info['channel_title'])} (<code>{html.escape(channel_id)}</code>)\n"
        f"🛠️ <b>Mod:</b> Bersedia untuk laporan automatik penerbitan Facebook &amp; amaran ralat."
    )

    text_msg_ch = (
        f"📢 <b>LAPORAN UJIAN CHANNEL :: PS2 RETRO NOSTALGIA</b>\n\n"
        f"✅ Channel ini berjaya disambungkan ke enjin automasi.\n"
        f"🤖 <b>Bot Pengendali:</b> {html.escape(v_info['bot_username'])}\n"
        f"🕒 <b>Masa MYT:</b> {html.escape(myt_now)}\n"
        f"📌 Setiap status hantaran baharu dan ralat sistem akan dilaporkan terus ke sini."
    )

    photo_caption = (
        f"🖼️ <b>UJIAN PENGHANTARAN MEDIA VISUAL TELEGRAM</b>\n\n"
        f"• <b>Fail:</b> <code>{html.escape(sample_img.name)}</code> ({img_kb} KB)\n"
        f"• <b>Sumber:</b> <code>temp/test_media_pool_50/raw/</code>\n"
        f"• <b>Masa MYT:</b> {html.escape(myt_now)}"
    )

    with console.status("[bold cyan]Menghantar mesej & gambar ujian ke DM Admin dan Channel Telegram...[/bold cyan]"):
        ok1, res1 = send_telegram_text(bot_token, dm_chat_id, text_msg_dm)
        results_rows.append(("1. Mesej Teks -> DM Admin", dm_chat_id, ok1, res1))

        ok2, res2 = send_telegram_text(bot_token, channel_id, text_msg_ch)
        results_rows.append(("2. Mesej Teks -> Channel", channel_id, ok2, res2))

        ok3, res3 = send_telegram_photo(bot_token, dm_chat_id, sample_img, photo_caption)
        results_rows.append((f"3. Gambar ({sample_img.name}) -> DM Admin", dm_chat_id, ok3, res3))

        ok4, res4 = send_telegram_photo(bot_token, channel_id, sample_img, photo_caption)
        results_rows.append((f"4. Gambar ({sample_img.name}) -> Channel", channel_id, ok4, res4))

    out_tbl = Table(
        title="3. Keputusan Penghantaran Mesej & Gambar Telegram (EXP07)",
        box=box.ROUNDED,
        header_style="bold cyan",
    )
    out_tbl.add_column("Ujian Penghantaran", style="bold white")
    out_tbl.add_column("Sasaran ID", style="cyan")
    out_tbl.add_column("Message ID", justify="center", style="yellow")
    out_tbl.add_column("Latensi", justify="right", style="dim")
    out_tbl.add_column("Status", justify="center")

    all_sent_ok = True
    for label, target, ok_flag, meta in results_rows:
        if not ok_flag:
            all_sent_ok = False
        out_tbl.add_row(
            label,
            target,
            str(meta.get("message_id", "-")),
            f"{meta.get('latency_ms', 0)} ms",
            "[bold green]BERJAYA[/bold green]" if ok_flag else f"[bold red]GAGAL: {meta.get('error', '')[:45]}[/bold red]",
        )
    console.print(out_tbl)

    if all_sent_ok:
        console.print(
            Panel.fit(
                "[bold green]✅ SEMUA 14 KUNCI TELEGRAM SAH & UJIAN MESEJ + GAMBAR BERJAYA 100%![/bold green]\n"
                "[dim]Sedia untuk dibina menjadi modul pengeluaran notifikasi & digabungkan ke X10/X11.[/dim]",
                border_style="green",
            )
        )
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()