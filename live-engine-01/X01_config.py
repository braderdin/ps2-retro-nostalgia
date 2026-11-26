#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - MODUL KONFIGURASI PUSAT (X01)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X01_config.py
#
# FUNGSI:
# 1. Membaca konfigurasi secara hibrid:
#    - Local WSL: /home/braderdin/ps2-retro-nostalgia/.env.local
#    - GitHub Actions: Repository Secrets (B2_ACCOUNTS_JSON, REDIS_ACCOUNTS_JSON,
#      VECTOR_ACCOUNTS_JSON, PS2N_FB_*, CF_WORKER_B2_PROXY_STORAGE, dll.)
# 2. Mengurus laluan model AI tempatan (/home/braderdin/X-HF-model-gguf) dan
#    fallback rujukan repositori Hugging Face untuk GitHub Actions pip/HF cache.
# 3. Menetapkan had kapasiti selamat free-tier & parameter auto-split 25MB-35MB.
# ==============================================================================

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

# ------------------------------------------------------------------------------
# 1. LALUAN DIREKTORI PROJEK & STORAN DATA
# ------------------------------------------------------------------------------
LIVE_ENGINE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = LIVE_ENGINE_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
TEMP_DIR = PROJECT_ROOT / "temp"
EXP_DIR = PROJECT_ROOT / "exp"

for _folder in (DATA_DIR, TEMP_DIR):
    _folder.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------------------
# 2. MUAT PEMBOLEH UBAH PERSEKITARAN (.env.local vs GITHUB ACTIONS)
# ------------------------------------------------------------------------------
ENV_LOCAL_PATH = PROJECT_ROOT / ".env.local"
IS_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS", "").lower() == "true"

if ENV_LOCAL_PATH.exists():
    # override=False memastikan pemboleh ubah dari GitHub Secrets / shell kekal diutamakan
    load_dotenv(dotenv_path=ENV_LOCAL_PATH, override=False)
else:
    load_dotenv(override=False)


def get_env(key: str, default: str = "") -> str:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().strip('"').strip("'")


# ------------------------------------------------------------------------------
# 3. PARAMETER AUTO-SPLIT FAIL (25 MB - 35 MB) & HAD FREE-TIER
# ------------------------------------------------------------------------------
SPLIT_MIN_BYTES = 25 * 1024 * 1024       # 25.0 MB
SPLIT_TARGET_BYTES = 30 * 1024 * 1024    # 30.0 MB (Titik tengah selamat 25MB-35MB)
SPLIT_MAX_BYTES = 35 * 1024 * 1024       # 35.0 MB

# Ambang selamat Free-Tier (5 Akaun B2, 5 Redis, 5 Vector)
B2_SAFE_BYTES_PER_ACCOUNT = int(9.0 * 1024 * 1024 * 1024)     # 9.0 GB / akaun (45 GB jumlah)
B2_CEILING_BYTES_PER_ACCOUNT = int(10.0 * 1024 * 1024 * 1024) # 10.0 GB / akaun (50 GB jumlah)
B2_SAFE_DAILY_OPS_PER_ACCOUNT = 2000                          # Rizab selamat transaksi B2/hari

REDIS_SAFE_DAILY_CMDS_PER_ACCOUNT = 8000                      # Rizab selamat daripada 10,000/hari
REDIS_MAX_MEMORY_BYTES = 256 * 1024 * 1024                    # 256 MB RAM / akaun

VECTOR_SAFE_DAILY_QUERIES_PER_ACCOUNT = 8000                  # Rizab selamat daripada 10,000/hari
VECTOR_MAX_RECORDS_PER_ACCOUNT = 9500                         # Rizab selamat daripada 10,000 vektor

# ------------------------------------------------------------------------------
# 4. KONFIGURASI FACEBOOK / META GRAPH API
# ------------------------------------------------------------------------------
PS2N_FB_APP_ID = get_env("PS2N_FB_APP_ID")
PS2N_FB_APP_SECRET = get_env("PS2N_FB_APP_SECRET")
PS2N_FB_PAGE_ID = get_env("PS2N_FB_PAGE_ID")
PS2N_FB_PAGE_TOKEN = get_env("PS2N_FB_PAGE_TOKEN")
PS2N_FB_GRAPH_VERSION = get_env("PS2N_FB_GRAPH_VERSION", "v26.0")
PS2N_FB_GRAPH_BASE = f"https://graph.facebook.com/{PS2N_FB_GRAPH_VERSION}"

# ------------------------------------------------------------------------------
# 5. KONFIGURASI CLOUDFLARE WORKER B2 PROXY & MEDIA / SHOPEE
# ------------------------------------------------------------------------------
CF_WORKER_B2_PROXY_STORAGE = get_env(
    "CF_WORKER_B2_PROXY_STORAGE",
    "https://ps2n-b2-proxy.braderdinphoto002.workers.dev",
).rstrip("/")
PS2N_PROXY_SECRET = get_env("PS2N_PROXY_SECRET")

PS2N_UNSPLASH_ACCESS_KEY = get_env("PS2N_UNSPLASH_ACCESS_KEY")
PS2N_PEXELS_API_KEY = get_env("PS2N_PEXELS_API_KEY")

# Pautan Kedai & Produk Shopee (Untuk Komen Pertama)
PS2N_SHOPEE_STORE_URL = get_env("PS2N_SHOPEE_STORE_URL", "https://shopee.com.my")
PS2N_SHOPEE_MC_URL = get_env("PS2N_SHOPEE_MC_URL", PS2N_SHOPEE_STORE_URL)
PS2N_SHOPEE_PENDRIVE_URL = get_env("PS2N_SHOPEE_PENDRIVE_URL", PS2N_SHOPEE_STORE_URL)

# ------------------------------------------------------------------------------
# 6. KONFIGURASI MODEL AI GGUF (LOCAL WSL vs GITHUB ACTIONS HF CACHE)
# ------------------------------------------------------------------------------
HF_REPO_ID = get_env("PS2N_HF_REPO_ID", "unsloth/Qwen3.5-4B-GGUF")
HF_MODEL_FILENAME = get_env("PS2N_HF_MODEL_FILENAME", "Qwen3.5-4B-Q4_K_M.gguf")
HF_MMPROJ_FILENAME = get_env("PS2N_HF_MMPROJ_FILENAME", "mmproj-F16.gguf")

PS2N_LOCAL_GGUF_DIR = Path(get_env("PS2N_LOCAL_GGUF_DIR", "/home/braderdin/X-HF-model-gguf"))
PS2N_LOCAL_LLM_MODEL = Path(
    get_env("PS2N_LOCAL_LLM_MODEL", str(PS2N_LOCAL_GGUF_DIR / HF_MODEL_FILENAME))
)
PS2N_LOCAL_LLM_MMPROJ = Path(
    get_env("PS2N_LOCAL_LLM_MMPROJ", str(PS2N_LOCAL_GGUF_DIR / HF_MMPROJ_FILENAME))
)


def resolve_gguf_paths() -> Dict[str, Any]:
    """
    Mengesan laluan fail model GGUF:
    - Jika fail tempatan wujud di /home/braderdin/X-HF-model-gguf/, guna terus.
    - Jika tiada (contohnya di GitHub Actions), pulangkan info Hugging Face Hub
      untuk dimuat turun melalui pip/HF cache.
    """
    local_ready = PS2N_LOCAL_LLM_MODEL.exists() and PS2N_LOCAL_LLM_MMPROJ.exists()
    return {
        "mode": "local_disk" if local_ready else "huggingface_cache",
        "local_ready": local_ready,
        "model_path": str(PS2N_LOCAL_LLM_MODEL),
        "mmproj_path": str(PS2N_LOCAL_LLM_MMPROJ),
        "hf_repo_id": HF_REPO_ID,
        "hf_model_filename": HF_MODEL_FILENAME,
        "hf_mmproj_filename": HF_MMPROJ_FILENAME,
    }


# ------------------------------------------------------------------------------
# 7. PENGEKSTRAKAN MULTI-AKAUN (B2, UPSTASH REDIS & UPSTASH VECTOR)
# ------------------------------------------------------------------------------
def _parse_json_secret(env_key: str) -> List[Dict[str, Any]]:
    raw = get_env(env_key)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    except Exception as exc:
        console.print(f"[bold yellow]⚠️ Amaran JSON tidak sah pada {env_key}: {exc}[/bold yellow]")
    return []


def get_b2_accounts() -> List[Dict[str, Any]]:
    """
    Keutamaan 1: GitHub Secret (B2_ACCOUNTS_JSON)
    Keutamaan 2: .env.local (B2_ACC1_KEY_ID .. B2_ACC5_KEY_ID)
    """
    accounts: List[Dict[str, Any]] = []
    json_items = _parse_json_secret("B2_ACCOUNTS_JSON")
    if json_items:
        for idx, item in enumerate(json_items, 1):
            key_id = str(item.get("key_id", "")).strip()
            app_key = str(item.get("app_key", "")).strip()
            bucket_name = str(item.get("bucket_name", "")).strip()
            if key_id and app_key and bucket_name:
                accounts.append({
                    "index": int(item.get("index", idx)),
                    "bucket_name": bucket_name,
                    "bucket_id": str(item.get("bucket_id", "")).strip(),
                    "key_name": str(item.get("key_name", f"key-{idx:03d}")).strip(),
                    "key_id": key_id,
                    "app_key": app_key,
                    "endpoint": str(item.get("endpoint", "s3.us-west-004.backblazeb2.com")).strip(),
                })
        if accounts:
            accounts.sort(key=lambda x: x["index"])
            return accounts

    for idx in range(1, 11):
        for fmt in (f"{idx}", f"{idx:02d}", f"{idx:03d}"):
            pfx = f"B2_ACC{fmt}_"
            key_id = get_env(f"{pfx}KEY_ID")
            app_key = get_env(f"{pfx}APP_KEY")
            bucket_name = get_env(f"{pfx}BUCKET_NAME")
            if key_id and app_key and bucket_name:
                accounts.append({
                    "index": len(accounts) + 1,
                    "bucket_name": bucket_name,
                    "bucket_id": get_env(f"{pfx}BUCKET_ID"),
                    "key_name": get_env(f"{pfx}KEY_NAME", f"key-{idx:03d}"),
                    "key_id": key_id,
                    "app_key": app_key,
                    "endpoint": get_env(f"{pfx}S3_API_ENDPOINT", "s3.us-west-004.backblazeb2.com"),
                })
                break
    return accounts


def get_redis_accounts() -> List[Dict[str, Any]]:
    """
    Keutamaan 1: GitHub Secret (REDIS_ACCOUNTS_JSON)
    Keutamaan 2: .env.local (UPSTASH_REDIS_001_REST_URL .. 005)
    """
    accounts: List[Dict[str, Any]] = []
    json_items = _parse_json_secret("REDIS_ACCOUNTS_JSON")
    if json_items:
        for idx, item in enumerate(json_items, 1):
            url = str(item.get("url") or item.get("redis_rest_url") or "").strip().rstrip("/")
            token = str(item.get("token") or item.get("redis_rest_token") or "").strip()
            if url and token:
                accounts.append({
                    "index": int(item.get("index", idx)),
                    "url": url,
                    "token": token,
                    "tcp_url": str(item.get("tcp_url", "")).strip(),
                })
        if accounts:
            accounts.sort(key=lambda x: x["index"])
            return accounts

    for idx in range(1, 11):
        for fmt in (f"{idx:03d}", f"{idx:02d}", f"{idx}"):
            url = get_env(f"UPSTASH_REDIS_{fmt}_REST_URL").rstrip("/")
            token = get_env(f"UPSTASH_REDIS_{fmt}_REST_TOKEN")
            if url and token:
                accounts.append({
                    "index": len(accounts) + 1,
                    "url": url,
                    "token": token,
                    "tcp_url": get_env(f"REDIS_{fmt}_TCP_URL"),
                })
                break
    return accounts


def get_vector_accounts() -> List[Dict[str, Any]]:
    """
    Keutamaan 1: GitHub Secret (VECTOR_ACCOUNTS_JSON)
    Keutamaan 2: .env.local (UPSTASH_VECTOR_001_REST_URL .. 005)
    """
    accounts: List[Dict[str, Any]] = []
    json_items = _parse_json_secret("VECTOR_ACCOUNTS_JSON")
    if json_items:
        for idx, item in enumerate(json_items, 1):
            url = str(item.get("url") or "").strip().rstrip("/")
            token = str(item.get("token") or "").strip()
            if url and token:
                accounts.append({
                    "index": int(item.get("index", idx)),
                    "url": url,
                    "token": token,
                })
        if accounts:
            accounts.sort(key=lambda x: x["index"])
            return accounts

    for idx in range(1, 11):
        for fmt in (f"{idx:03d}", f"{idx:02d}", f"{idx}"):
            url = get_env(f"UPSTASH_VECTOR_{fmt}_REST_URL").rstrip("/")
            token = get_env(f"UPSTASH_VECTOR_{fmt}_REST_TOKEN")
            if url and token:
                accounts.append({
                    "index": len(accounts) + 1,
                    "url": url,
                    "token": token,
                })
                break
    return accounts


B2_ACCOUNTS = get_b2_accounts()
REDIS_ACCOUNTS = get_redis_accounts()
VECTOR_ACCOUNTS = get_vector_accounts()


# ------------------------------------------------------------------------------
# 8. DIAGNOSTIK KENDIRI (APABILA DIJALANKAN SECARA TERUS)
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    mode_label = "GITHUB ACTIONS (SECRETS)" if IS_GITHUB_ACTIONS else "LOCAL WSL (.env.local)"
    gguf_info = resolve_gguf_paths()

    console.print(
        Panel.fit(
            f"[bold cyan]PS2-RETRO-NOSTALGIA :: CENTRAL CONFIG VERIFICATION (X01)[/bold cyan]\n"
            f"[dim]Mod Persekitaran: {mode_label} | Root: {PROJECT_ROOT}[/dim]",
            border_style="cyan",
        )
    )

    tbl = Table(box=box.ROUNDED, header_style="bold magenta")
    tbl.add_column("Komponen Konfigurasi", style="bold white")
    tbl.add_column("Nilai / Laluan", style="cyan")
    tbl.add_column("Status", justify="center")

    tbl.add_row("Folder Data Kekal", str(DATA_DIR), "[green]SEDIA[/green]")
    tbl.add_row("Folder Data Sementara", str(TEMP_DIR), "[green]SEDIA[/green]")
    tbl.add_row(
        "Had Auto-Split Fail",
        f"Min 25 MB | Sasaran {SPLIT_TARGET_BYTES // (1024 * 1024)} MB | Maks 35 MB",
        "[green]AKTIF[/green]",
    )
    tbl.add_row(
        "Facebook Page API",
        f"Page ID: {PS2N_FB_PAGE_ID or '-'} ({PS2N_FB_GRAPH_VERSION})",
        "[bold green]OK[/bold green]" if (PS2N_FB_PAGE_ID and PS2N_FB_PAGE_TOKEN) else "[bold red]TIADA[/bold red]",
    )
    tbl.add_row(
        "Cloudflare B2 Proxy",
        CF_WORKER_B2_PROXY_STORAGE,
        "[bold green]OK[/bold green]" if PS2N_PROXY_SECRET else "[bold red]TIADA SECRET[/bold red]",
    )
    tbl.add_row(
        "Backblaze B2 Accounts",
        f"{len(B2_ACCOUNTS)} baldi ({len(B2_ACCOUNTS) * 9} GB had selamat)",
        "[bold green]5/5[/bold green]" if len(B2_ACCOUNTS) == 5 else f"[yellow]{len(B2_ACCOUNTS)}[/yellow]",
    )
    tbl.add_row(
        "Upstash Redis Shards",
        f"{len(REDIS_ACCOUNTS)} shard ({len(REDIS_ACCOUNTS) * 10000:,} cmds/hari)",
        "[bold green]5/5[/bold green]" if len(REDIS_ACCOUNTS) == 5 else f"[yellow]{len(REDIS_ACCOUNTS)}[/yellow]",
    )
    tbl.add_row(
        "Upstash Vector Indexes",
        f"{len(VECTOR_ACCOUNTS)} indeks ({len(VECTOR_ACCOUNTS) * 10000:,} carian/hari)",
        "[bold green]5/5[/bold green]" if len(VECTOR_ACCOUNTS) == 5 else f"[yellow]{len(VECTOR_ACCOUNTS)}[/yellow]",
    )
    tbl.add_row(
        "Local LLM GGUF",
        f"{gguf_info['mode']} -> {Path(gguf_info['model_path']).name}",
        "[bold green]FAIL WUJUD[/bold green]" if gguf_info["local_ready"] else "[yellow]HF CACHE MODE[/yellow]",
    )

    console.print(tbl)