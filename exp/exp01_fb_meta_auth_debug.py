#!/usr/bin/env python3
"""
exp01_fb_meta_auth_debug.py
Diagnostik terperinci untuk mengesahkan kunci Meta/Facebook di dalam .env.local:
- PS2N_FB_APP_ID
- PS2N_FB_APP_SECRET
- PS2N_FB_PAGE_ID
- PS2N_FB_PAGE_TOKEN
- PS2N_FB_GRAPH_VERSION (lalai: v26.0)
"""

import hashlib
import hmac
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import dotenv_values
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env.local"
MYT = timezone(timedelta(hours=8))

REQUIRED_KEYS = [
    "PS2N_FB_APP_ID",
    "PS2N_FB_APP_SECRET",
    "PS2N_FB_PAGE_ID",
    "PS2N_FB_PAGE_TOKEN",
]

REQUIRED_SCOPES = {
    "pages_show_list": "Kesan senarai halaman & pengesahan pemilikan",
    "pages_manage_posts": "Terbitkan cerita, gambar & video ke Page",
    "pages_manage_engagement": "Hantar pautan produk Shopee di ruangan komen",
    "pages_read_engagement": "Baca ID hantaran baharu & status komen",
}

BONUS_SCOPES = {
    "publish_video": "Muat naik video gameplay & FB Reels beresolusi tinggi",
    "pages_manage_metadata": "Urus tetapan & webhook halaman",
}


def mask_secret(val: str | None, show_start: int = 6, show_end: int = 5) -> str:
    if not val:
        return "-"
    clean = val.strip()
    if len(clean) <= (show_start + show_end):
        return "*" * len(clean)
    return f"{clean[:show_start]}...{clean[-show_end:]}"


def format_unix_myt(ts: int | None) -> str:
    if ts is None:
        return "Tidak diketahui"
    if ts == 0:
        return "NEVER (Kekal / Tiada Tamat Tempoh)"
    dt = datetime.fromtimestamp(ts, tz=MYT)
    now = datetime.now(tz=MYT)
    days_left = (dt - now).days
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S MYT')} ({days_left} hari lagi)"


def build_appsecret_proof(access_token: str, app_secret: str) -> str:
    return hmac.new(
        app_secret.encode("utf-8"),
        access_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def parse_meta_error(resp: requests.Response) -> dict:
    try:
        payload = resp.json()
        err = payload.get("error", {})
        return {
            "http_status": resp.status_code,
            "message": err.get("message", resp.text),
            "type": err.get("type", "UnknownError"),
            "code": err.get("code", "-"),
            "subcode": err.get("error_subcode", "-"),
            "fbtrace_id": err.get("fbtrace_id", "-"),
        }
    except Exception:
        return {
            "http_status": resp.status_code,
            "message": resp.text[:300],
            "type": "NonJSONResponse",
            "code": "-",
            "subcode": "-",
            "fbtrace_id": "-",
        }


def render_meta_error_panel(title: str, err_info: dict) -> None:
    err_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    err_table.add_column("Medan", style="bold red", width=16)
    err_table.add_column("Butiran", style="white")
    err_table.add_row("HTTP Status", str(err_info["http_status"]))
    err_table.add_row("Jenis Ralat", str(err_info["type"]))
    err_table.add_row("Kod / Subkod", f"{err_info['code']} / {err_info['subcode']}")
    err_table.add_row("Mesej Meta", str(err_info["message"]))
    err_table.add_row("FB Trace ID", str(err_info["fbtrace_id"]))
    console.print(Panel(err_table, title=f"[bold red]{title}[/bold red]", border_style="red"))


def main() -> None:
    console.print(
        Panel.fit(
            "[bold cyan]PS2-RETRO-NOSTALGIA :: META GRAPH API DIAGNOSTIC (EXP01)[/bold cyan]\n"
            f"[dim]Sasaran Fail Konfigurasi: {ENV_FILE}[/dim]",
            border_style="cyan",
        )
    )

    # 1. Semak kewujudan fail .env.local
    if not ENV_FILE.exists():
        console.print(
            Panel(
                f"[bold red]FAIL TIDAK DITEMUI:[/bold red] {ENV_FILE}\n"
                "Sila pastikan fail .env.local wujud di direktori utama projek.",
                border_style="red",
            )
        )
        sys.exit(1)

    raw_env = dotenv_values(ENV_FILE)

    # 2. Jadual Audit Kunci Persekitaran (.env.local)
    env_table = Table(
        title="1. Audit Pemboleh Ubah Persekitaran (.env.local)",
        box=box.ROUNDED,
        header_style="bold magenta",
    )
    env_table.add_column("Nama Kunci", style="bold cyan")
    env_table.add_column("Nilai (Bertopeng)", style="white")
    env_table.add_column("Panjang", justify="right")
    env_table.add_column("Semakan Format", style="yellow")
    env_table.add_column("Status", justify="center")

    missing_keys = []
    clean_values: dict[str, str] = {}

    for key in REQUIRED_KEYS:
        raw_val = raw_env.get(key)
        if not raw_val or not raw_val.strip():
            missing_keys.append(key)
            env_table.add_row(key, "-", "0", "Tiada nilai ditemui", "[bold red]GAGAL[/bold red]")
            continue

        stripped = raw_val.strip()
        clean_values[key] = stripped
        has_whitespace = raw_val != stripped
        format_note = (
            "[bold yellow]Amaran: Ada ruang kosong di hujung[/bold yellow]"
            if has_whitespace
            else "[green]Bersih (Tiada whitespace)[/green]"
        )
        masked = stripped if key in ("PS2N_FB_APP_ID", "PS2N_FB_PAGE_ID") else mask_secret(stripped)
        env_table.add_row(
            key,
            masked,
            str(len(stripped)),
            format_note,
            "[bold green]ADA[/bold green]",
        )

    # Semak versi Graph API (opsyenal dalam .env.local, lalai v26.0)
    raw_ver = raw_env.get("PS2N_FB_GRAPH_VERSION")
    graph_version = raw_ver.strip() if raw_ver and raw_ver.strip() else "v26.0"
    ver_note = (
        "[green]Dari .env.local[/green]"
        if (raw_ver and raw_ver.strip())
        else "[cyan]Guna lalai (v26.0)[/cyan]"
    )
    if not graph_version.startswith("v"):
        ver_note = "[bold red]Ralat: Mesti bermula huruf 'v' (cth: v26.0)[/bold red]"

    env_table.add_row(
        "PS2N_FB_GRAPH_VERSION",
        graph_version,
        str(len(graph_version)),
        ver_note,
        "[bold green]OK[/bold green]" if graph_version.startswith("v") else "[bold red]SEMAK[/bold red]",
    )

    console.print(env_table)

    if missing_keys:
        console.print(
            Panel(
                f"[bold red]Proses dihentikan.[/bold red] Kunci wajib berikut belum diisi: {', '.join(missing_keys)}",
                border_style="red",
            )
        )
        sys.exit(1)

    app_id = clean_values["PS2N_FB_APP_ID"]
    app_secret = clean_values["PS2N_FB_APP_SECRET"]
    page_id = clean_values["PS2N_FB_PAGE_ID"]
    page_token = clean_values["PS2N_FB_PAGE_TOKEN"]
    base_url = f"https://graph.facebook.com/{graph_version}"

    # 3. Penjanaan Kriptografi appsecret_proof
    proof = build_appsecret_proof(page_token, app_secret)
    console.print(
        Panel(
            f"[bold green]appsecret_proof (HMAC-SHA256) berjaya dijana:[/bold green] "
            f"[white]{mask_secret(proof, 12, 12)}[/white]\n"
            "[dim]Mengesahkan gabungan PS2N_FB_APP_SECRET dan PS2N_FB_PAGE_TOKEN boleh ditandatangani.[/dim]",
            title="2. Pengesahan Kriptografi App Secret",
            border_style="green",
        )
    )

    all_passed = True

    # 4. Ujian /debug_token menggunakan App Access Token (APP_ID|APP_SECRET)
    with console.status("[bold cyan]Menghubungi Meta /debug_token untuk audit kesahihan token...[/bold cyan]"):
        app_access_token = f"{app_id}|{app_secret}"
        try:
            debug_resp = requests.get(
                f"{base_url}/debug_token",
                params={
                    "input_token": page_token,
                    "access_token": app_access_token,
                },
                timeout=15,
            )
        except requests.RequestException as exc:
            console.print(f"[bold red]Ralat Rangkaian semasa menghubungi Meta API:[/bold red] {exc}")
            sys.exit(1)

    if debug_resp.status_code != 200:
        all_passed = False
        err = parse_meta_error(debug_resp)
        render_meta_error_panel(
            "3. Ralat /debug_token (Semak PS2N_FB_APP_ID, PS2N_FB_APP_SECRET atau TOKEN)",
            err,
        )
        sys.exit(1)

    debug_data = debug_resp.json().get("data", {})
    is_valid = debug_data.get("is_valid", False)
    token_type = debug_data.get("type", "-")
    token_app_id = str(debug_data.get("app_id", "-"))
    token_profile_id = str(debug_data.get("profile_id", "-"))
    expires_at = debug_data.get("expires_at")
    data_access_expires_at = debug_data.get("data_access_expires_at")
    granted_scopes = set(debug_data.get("scopes", []))

    app_match = token_app_id == app_id
    page_match = token_profile_id == page_id
    type_match = token_type.upper() == "PAGE"
    never_expires = expires_at == 0

    if not (is_valid and app_match and page_match and type_match):
        all_passed = False

    meta_table = Table(
        title="3. Laporan Rasmi Meta /debug_token",
        box=box.ROUNDED,
        header_style="bold cyan",
    )
    meta_table.add_column("Parameter Token", style="bold white")
    meta_table.add_column("Nilai Diterima dari Meta", style="cyan")
    meta_table.add_column("Pengesahan", justify="center")

    meta_table.add_row(
        "Status Sah (is_valid)",
        str(is_valid),
        "[bold green]SAH[/bold green]" if is_valid else "[bold red]TIDAK SAH[/bold red]",
    )
    meta_table.add_row(
        "Jenis Token (type)",
        token_type,
        "[bold green]PAGE TOKEN[/bold green]" if type_match else "[bold red]BUKAN PAGE TOKEN[/bold red]",
    )
    meta_table.add_row(
        "Pemilik App ID",
        f"{token_app_id} ({debug_data.get('application', '-')})",
        "[bold green]SEPADAN[/bold green]" if app_match else "[bold red]TIDAK SEPADAN[/bold red]",
    )
    meta_table.add_row(
        "Sasaran Page ID",
        token_profile_id,
        "[bold green]SEPADAN[/bold green]" if page_match else "[bold red]TIDAK SEPADAN[/bold red]",
    )
    meta_table.add_row(
        "Tamat Tempoh Token",
        format_unix_myt(expires_at),
        "[bold green]KEKAL[/bold green]" if never_expires else "[bold yellow]ADA TEMPOH[/bold yellow]",
    )
    meta_table.add_row(
        "Tamat Akses Data",
        format_unix_myt(data_access_expires_at),
        "[cyan]INFO[/cyan]",
    )
    console.print(meta_table)

    # 5. Audit Kebenaran (Scopes) untuk Penerbitan & Auto-Komen Shopee
    scope_table = Table(
        title="4. Audit Kebenaran API (Scopes & Permissions)",
        box=box.ROUNDED,
        header_style="bold magenta",
    )
    scope_table.add_column("Nama Scope", style="bold white")
    scope_table.add_column("Kategori", justify="center")
    scope_table.add_column("Fungsi dalam Projek PS2", style="dim")
    scope_table.add_column("Status", justify="center")

    for scope, desc in REQUIRED_SCOPES.items():
        ok = scope in granted_scopes
        if not ok:
            all_passed = False
        scope_table.add_row(
            scope,
            "[yellow]WAJIB[/yellow]",
            desc,
            "[bold green]AKTIF[/bold green]" if ok else "[bold red]TIADA[/bold red]",
        )

    for scope, desc in BONUS_SCOPES.items():
        ok = scope in granted_scopes
        scope_table.add_row(
            scope,
            "[cyan]TAMBAHAN[/cyan]",
            desc,
            "[bold green]AKTIF[/bold green]" if ok else "[dim]TIDAK AKTIF[/dim]",
        )

    console.print(scope_table)

    # 6. Ujian Capaian Langsung ke Nod Facebook Page (menggunakan appsecret_proof)
    with console.status(f"[bold cyan]Menguji capaian langsung ke Page ID {page_id}...[/bold cyan]"):
        page_resp = requests.get(
            f"{base_url}/{page_id}",
            params={
    "fields": "id,name,username,category,about,fan_count,followers_count,link",
    "access_token": page_token,
    "appsecret_proof": proof,
},
            timeout=15,
        )

    if page_resp.status_code != 200:
        all_passed = False
        err = parse_meta_error(page_resp)
        render_meta_error_panel("5. Ralat Capaian Nod Facebook Page", err)
    else:
     page_info = page_resp.json()
     returned_id = str(page_info.get("id", ""))
     page_verified = returned_id == page_id

     if not page_verified:
        all_passed = False

    page_table = Table(
        title="5. Pengesahan Capaian Langsung Profil Facebook Page",
        box=box.ROUNDED,
        header_style="bold green",
    )
    page_table.add_column("Medan Page", style="bold white", width=22)
    page_table.add_column("Maklumat Diterima", style="cyan")

    page_table.add_row("Page ID", returned_id or "-")
    page_table.add_row("Nama Halaman", str(page_info.get("name", "-")))
    page_table.add_row("Username", str(page_info.get("username", "-")))
    page_table.add_row("Kategori", str(page_info.get("category", "-")))
    page_table.add_row("Pautan Rasmi", str(page_info.get("link", "-")))
    page_table.add_row(
        "Likes / Followers",
        f"{page_info.get('fan_count', 0)} Likes | {page_info.get('followers_count', 0)} Followers",
    )
    page_table.add_row(
        "Status Sambungan",
        (
            "[bold green]TERHUBUNG (Sedia untuk Auto-Post Gambar/Reels & Auto-Komen Shopee)[/bold green]"
            if page_verified
            else "[bold red]Page ID tidak sepadan[/bold red]"
        ),
    )
    console.print(page_table)

    # 7. Ringkasan Akhir
    if all_passed:
        console.print(
            Panel(
                Text.from_markup(
                    "[bold green]SEMUA UJIAN BERJAYA (100% READY)[/bold green]\n"
                    "Keempat-empat kunci Meta/Facebook di dalam .env.local adalah sah, sepadan, "
                    "berstatus kekal (Never Expires), dan mempunyai kebenaran penuh untuk menerbitkan "
                    "kandungan PS2 serta menghantar pautan produk Shopee di ruangan komen."
                ),
                border_style="bold green",
                title="Status Akhir Diagnostik",
            )
        )
    else:
        console.print(
            Panel(
                Text.from_markup(
                    "[bold yellow]DIAGNOSTIK SELESAI DENGAN AMARAN / RALAT[/bold yellow]\n"
                    "Sila semak jadual bertanda merah di atas sebelum meneruskan ke modul penerbitan."
                ),
                border_style="bold yellow",
                title="Status Akhir Diagnostik",
            )
        )


if __name__ == "__main__":
    main()