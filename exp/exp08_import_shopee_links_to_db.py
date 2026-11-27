#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - IMPORT KATALOG SHOPEE .XLSX KE SQLITE3 .DB (EXP08)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/exp/exp08_import_shopee_links_to_db.py
#
# FUNGSI UTAMA:
# 1. Membaca kesemua baris produk & pautan Shopee daripada:
#    /home/braderdin/ps2-retro-nostalgia/text-archive/ps2-links-shopee.xlsx
# 2. Mengklasifikasikan keserasian setiap produk secara automatik:
#    - MATRIX_ONLY      : Khas untuk PS2 Modchip Matrix (logo Matrix masa ON)
#    - NON_MATRIX_OPL   : Set OPL lengkap (Mod Card FMCB/OpenTuna + Pendrive/HDD)
#    - MOD_CARD_ONLY    : Kad memori sistem FMCB / OpenTuna / Fortuna + Code Breaker
#    - UNIVERSAL_STORAGE: Pendrive / HDD umum (boleh pilih senarai game)
# 3. Menyimpan rekod ke /home/braderdin/ps2-retro-nostalgia/data/ps2_shopee_products.db
#    berserta nota teknikal daripada fail .txt di text-archive/ (niche PS2 sahaja).
# ==============================================================================

import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ------------------------------------------------------------------------------
# LALUAN FAIL SUMBER & PANGKALAN DATA SASARAN
# ------------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
TEXT_ARCHIVE_DIR = PROJECT_ROOT / "text-archive"
DATA_DIR = PROJECT_ROOT / "data"

SOURCE_XLSX_PATH = TEXT_ARCHIVE_DIR / "ps2-links-shopee.xlsx"
TARGET_DB_PATH = DATA_DIR / "ps2_shopee_products.db"

MYT = timezone(timedelta(hours=8))


def read_shopee_xlsx_rows(xlsx_path: Path) -> List[Tuple[str, str]]:
    """
    Membaca semua pasangan (nama_produk, pautan_shopee) daripada fail .xlsx
    tanpa tertinggal baris pertama walaupun fail tiada baris header.
    """
    if not xlsx_path.exists():
        raise FileNotFoundError(f"Fail sumber tidak ditemui: {xlsx_path}")

    try:
        openpyxl: Any = __import__("openpyxl")
    except ImportError as exc:
        raise RuntimeError(
            "Pustaka 'openpyxl' belum dipasang dalam venv. Jalankan: pip install openpyxl"
        ) from exc

    wb = openpyxl.load_workbook(filename=str(xlsx_path), read_only=True, data_only=True)
    ws = wb.active

    extracted: List[Tuple[str, str]] = []
    for row in ws.iter_rows(values_only=True):
        if not row or len(row) < 2:
            continue
        col_name = str(row[0] or "").strip()
        col_url = str(row[1] or "").strip()

        if not col_name or not col_url:
            continue
        # Langkau baris jika bukan pautan HTTP/HTTPS yang sah
        if not col_url.lower().startswith(("http://", "https://")):
            continue

        extracted.append((col_name, col_url))

    wb.close()
    return extracted


def classify_ps2_product(product_name: str, shopee_url: str, index_num: int) -> Dict[str, Any]:
    """
    Menganalisis tajuk produk Shopee untuk menentukan kategori keserasian PS2,
    kapasiti storan, kata kunci padanan cerita AI, dan ayat CTA komen pertama.
    """
    low = product_name.lower()
    product_code = f"SHP_{index_num:03d}"

    is_matrix = "matrix" in low
    has_mod_card = any(k in low for k in ["mod card", "fmcb", "opentuna", "fortuna", "freemcboot"])
    has_pendrive = "pendrive" in low or "usb" in low
    has_hdd = "hdd" in low or "hardisk" in low or "hard disk" in low

    # 1. Tentukan Kategori Keserasian
    if is_matrix:
        compatibility = "MATRIX_ONLY"
        includes_mod_card = 0
        requires_mod_card = 0
        game_list_url = "https://bit.ly/3fgGyQa"
    elif has_mod_card and not (has_pendrive and ("32gb" in low or "64gb" in low)):
        compatibility = "MOD_CARD_ONLY"
        includes_mod_card = 1
        requires_mod_card = 0
        game_list_url = "https://bit.ly/3K3g12H"
    elif has_mod_card:
        compatibility = "NON_MATRIX_OPL_SET"
        includes_mod_card = 1
        requires_mod_card = 0
        game_list_url = "https://bit.ly/3Bq7FhH"
    else:
        compatibility = "UNIVERSAL_STORAGE"
        includes_mod_card = 0
        requires_mod_card = 1
        game_list_url = "https://bit.ly/3K3g12H"

    # 2. Tentukan Kapasiti Storan & Anggaran Bilangan Game
    if compatibility == "MOD_CARD_ONLY":
        storage_tier = "MOD_CARD_8MB_64MB"
        game_count_hint = "Sistem Boot OPL (FMCB / OpenTuna + Code Breaker)"
    elif "320gb" in low:
        storage_tier = "HDD_320GB"
        game_count_hint = "80 - 130 game PS2"
    elif "250gb" in low or "250 gb" in low:
        storage_tier = "HDD_250GB"
        game_count_hint = "60 - 80 game PS2"
    elif "64gb" in low and not has_hdd:
        storage_tier = "PENDRIVE_64GB"
        game_count_hint = "10 - 20 game PS2"
    elif "32gb" in low and not has_hdd:
        storage_tier = "PENDRIVE_32GB"
        game_count_hint = "5 - 15 game PS2"
    else:
        storage_tier = "PENDRIVE_OR_HDD_MULTI"
        game_count_hint = "32GB (5-15 game) hingga 500GB (120-170 game)"

    # 3. Bina Kata Kunci & Ayat CTA Komen Pertama yang Tepat
    if compatibility == "MATRIX_ONLY":
        keywords = "matrix,modchip,pendrive,hardisk,hdd,plug and play,cd calar,laser rosak"
        cta_comment = (
            f"Kalau PS2 korang jenis Matrix (keluar logo Matrix masa ON), terus cucuk {storage_tier.replace('_', ' ')} "
            f"siap isi {game_count_hint} pilihan sendiri kat sini geng:\n👉 {shopee_url}"
        )
    elif compatibility == "NON_MATRIX_OPL_SET":
        keywords = "laser rosak,lens rosak,fmcb,opentuna,mod card,fat,slim,pendrive,memory card"
        cta_comment = (
            f"PS2 kat rumah lens dah tak baca disk? Guna set lengkap {storage_tier.replace('_', ' ')} + Mod Card "
            f"(FMCB untuk PS2 Fat / OpenTuna untuk PS2 Slim, siap {game_count_hint}) kat sini:\n👉 {shopee_url}"
        )
    elif compatibility == "MOD_CARD_ONLY":
        keywords = "memory card,8mb,save data,fmcb,opentuna,fortuna,code breaker,cheat,kod cheat"
        cta_comment = (
            f"Dah ada pendrive/HDD sendiri atau nak kad sistem siap Code Breaker (cheat)? "
            f"Boleh pilih Mod Card FMCB (PS2 Fat) / OpenTuna (PS2 Slim) kat sini:\n👉 {shopee_url}"
        )
    else:
        keywords = "pilih game,custom game,nostalgia,pendrive,hardisk,koleksi game ps2"
        cta_comment = (
            f"Rindu nak layan balik game klasik PS2 pilihan zaman sekolah dulu? "
            f"Boleh pilih sendiri senarai game siap masuk Pendrive / HDD kat sini geng:\n👉 {shopee_url}"
        )

    return {
        "product_code": product_code,
        "product_name": product_name,
        "shopee_url": shopee_url,
        "compatibility": compatibility,
        "storage_tier": storage_tier,
        "game_count_hint": game_count_hint,
        "includes_mod_card": includes_mod_card,
        "requires_mod_card": requires_mod_card,
        "game_list_url": game_list_url,
        "target_keywords": keywords,
        "cta_comment_template": cta_comment,
    }


def collect_ps2_reference_notes(archive_dir: Path) -> List[Dict[str, str]]:
    """
    Menyalin nota rujukan teknikal daripada fail .txt PS2 di text-archive/
    (mengecualikan fail luar niche PS2 seperti anime Naruto).
    """
    notes: List[Dict[str, str]] = []
    if not archive_dir.exists():
        return notes

    for txt_file in sorted(archive_dir.glob("*.txt")):
        fname_low = txt_file.name.lower()
        # Tapis keluar fail bukan PS2 (contoh: Naruto anime) atau salinan pendua '(1)'
        if "naruto" in fname_low or "(1)" in fname_low:
            continue
        if "ps2" not in fname_low and "matrix" not in fname_low and "fortuna" not in fname_low:
            continue

        content = txt_file.read_text(encoding="utf-8", errors="ignore").strip()
        if content:
            notes.append(
                {
                    "source_filename": txt_file.name,
                    "content": content,
                }
            )
    return notes


def save_to_sqlite_db(
    products: List[Dict[str, Any]],
    ref_notes: List[Dict[str, str]],
    db_path: Path,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(tz=MYT).isoformat()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS shopee_products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_code TEXT NOT NULL UNIQUE,
                product_name TEXT NOT NULL,
                shopee_url TEXT NOT NULL UNIQUE,
                compatibility TEXT NOT NULL,
                storage_tier TEXT NOT NULL,
                game_count_hint TEXT NOT NULL,
                includes_mod_card INTEGER DEFAULT 0,
                requires_mod_card INTEGER DEFAULT 0,
                game_list_url TEXT DEFAULT '',
                target_keywords TEXT DEFAULT '',
                cta_comment_template TEXT NOT NULL,
                times_promoted INTEGER DEFAULT 0,
                last_promoted_at TEXT DEFAULT NULL,
                is_active INTEGER DEFAULT 1,
                updated_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS product_reference_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_filename TEXT NOT NULL UNIQUE,
                content TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        for p in products:
            conn.execute(
                """
                INSERT INTO shopee_products (
                    product_code, product_name, shopee_url, compatibility,
                    storage_tier, game_count_hint, includes_mod_card,
                    requires_mod_card, game_list_url, target_keywords,
                    cta_comment_template, is_active, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(product_code) DO UPDATE SET
                    product_name = excluded.product_name,
                    shopee_url = excluded.shopee_url,
                    compatibility = excluded.compatibility,
                    storage_tier = excluded.storage_tier,
                    game_count_hint = excluded.game_count_hint,
                    includes_mod_card = excluded.includes_mod_card,
                    requires_mod_card = excluded.requires_mod_card,
                    game_list_url = excluded.game_list_url,
                    target_keywords = excluded.target_keywords,
                    cta_comment_template = excluded.cta_comment_template,
                    is_active = 1,
                    updated_at = excluded.updated_at
                """,
                (
                    p["product_code"],
                    p["product_name"],
                    p["shopee_url"],
                    p["compatibility"],
                    p["storage_tier"],
                    p["game_count_hint"],
                    p["includes_mod_card"],
                    p["requires_mod_card"],
                    p["game_list_url"],
                    p["target_keywords"],
                    p["cta_comment_template"],
                    now_iso,
                ),
            )

        for n in ref_notes:
            conn.execute(
                """
                INSERT INTO product_reference_notes (source_filename, content, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_filename) DO UPDATE SET
                    content = excluded.content,
                    updated_at = excluded.updated_at
                """,
                (n["source_filename"], n["content"], now_iso),
            )

        conn.commit()
    finally:
        conn.close()


def main() -> None:
    console.print(
        Panel.fit(
            "[bold cyan]PS2-RETRO-NOSTALGIA :: IMPORT SHOPEE .XLSX TO SQLITE3 .DB (EXP08)[/bold cyan]\n"
            f"[dim]Sumber Excel: {SOURCE_XLSX_PATH}\nOutput SQLite: {TARGET_DB_PATH}[/dim]",
            border_style="cyan",
        )
    )

    try:
        raw_rows = read_shopee_xlsx_rows(SOURCE_XLSX_PATH)
    except Exception as exc:
        console.print(f"[bold red]❌ Ralat membaca fail Excel: {exc}[/bold red]")
        sys.exit(1)

    if not raw_rows:
        console.print("[bold red]❌ Tiada baris produk sah ditemui dalam ps2-links-shopee.xlsx.[/bold red]")
        sys.exit(1)

    classified_products: List[Dict[str, Any]] = []
    for idx, (name, url) in enumerate(raw_rows, 1):
        classified_products.append(classify_ps2_product(name, url, idx))

    ref_notes = collect_ps2_reference_notes(TEXT_ARCHIVE_DIR)
    save_to_sqlite_db(classified_products, ref_notes, TARGET_DB_PATH)

    # Sahkan semula dengan membaca terus dari fail .db yang baru dijana
    conn = sqlite3.connect(str(TARGET_DB_PATH))
    conn.row_factory = sqlite3.Row
    db_rows = conn.execute("SELECT * FROM shopee_products ORDER BY id ASC").fetchall()
    note_count = conn.execute("SELECT COUNT(*) AS c FROM product_reference_notes").fetchone()["c"]
    conn.close()

    tbl = Table(
        title=f"Rekod Produk Shopee Disimpan dalam {TARGET_DB_PATH.name} ({len(db_rows)} Produk)",
        box=box.ROUNDED,
        header_style="bold green",
    )
    tbl.add_column("Kod", style="cyan", width=8)
    tbl.add_column("Keserasian", style="magenta", width=19)
    tbl.add_column("Storan / Kapasiti", style="yellow", width=22)
    tbl.add_column("Nama Produk Shopee", style="white")
    tbl.add_column("Pautan Shopee", style="bold blue")

    for r in db_rows:
        short_name = r["product_name"] if len(r["product_name"]) <= 46 else f"{r['product_name'][:43]}..."
        tbl.add_row(
            r["product_code"],
            r["compatibility"],
            f"{r['storage_tier']}\n({r['game_count_hint']})",
            short_name,
            r["shopee_url"],
        )

    console.print(tbl)
    db_kb = round(TARGET_DB_PATH.stat().st_size / 1024, 2)
    console.print(
        Panel.fit(
            f"[bold green]✅ BERJAYA DISIMPAN KE PANGKALAN DATA SQLITE3![/bold green]\n"
            f"• Fail DB: [cyan]{TARGET_DB_PATH}[/cyan] ({db_kb} KB)\n"
            f"• Jadual [yellow]shopee_products[/yellow]: [bold white]{len(db_rows)} produk aktif[/bold white]\n"
            f"• Jadual [yellow]product_reference_notes[/yellow]: [bold white]{note_count} fail nota teknikal PS2[/bold white]",
            border_style="green",
        )
    )


if __name__ == "__main__":
    main()