#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - PUSAT KONFIGURASI KUNCI TELEGRAM (X10)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X10_telegram_config.py
#
# FUNGSI UTAMA:
# 1. Modul berpusat untuk membaca semua kredensial Bot, DM Admin, Multi-Channel,
#    dan MTProto API Telegram daripada:
#    - GitHub Actions Secret: TELEGRAM_ALL_KEY_JSON (utamakan jika wujud di env)
#    - Persekitaran Tempatan WSL: /home/braderdin/ps2-retro-nostalgia/.env.local
# 2. Menyokong pengurusan multi-channel (1 bot yang sama boleh menghantar laporan
#    ke pelbagai channel mengikut alias seperti 'default', 'fb', 'error', dll.).
# 3. Sedia diimport oleh mana-mana modul laporan atau penerbitan melalui:
#    `from X10_telegram_config import tg_config`
# ==============================================================================

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import dotenv_values, load_dotenv

# ------------------------------------------------------------------------------
# LALUAN PROJEK & FAIL .env.local
# ------------------------------------------------------------------------------
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
DEFAULT_ENV_LOCAL_PATH = PROJECT_ROOT / ".env.local"


def _parse_extra_channels_from_env(env_map: Dict[str, str]) -> Dict[str, Dict[str, str]]:
    """
    Mengekstrak sebarang channel tambahan yang ditakrifkan melalui
    PS2N_TG_EXTRA_CHANNELS_JSON atau PS2N_TG_CHANNEL_2_ID.._10_ID.
    """
    extra_channels: Dict[str, Dict[str, str]] = {}

    extra_raw = env_map.get("PS2N_TG_EXTRA_CHANNELS_JSON", "").strip()
    if extra_raw:
        try:
            parsed = json.loads(extra_raw)
            if isinstance(parsed, dict):
                for alias, val in parsed.items():
                    alias_clean = re.sub(r"[^a-zA-Z0-9_-]", "", str(alias)).lower()
                    if not alias_clean:
                        continue
                    if isinstance(val, dict):
                        cid = str(val.get("chat_id") or val.get("id") or "").strip()
                        if cid:
                            extra_channels[alias_clean] = {
                                "name": str(val.get("name") or alias_clean.upper()),
                                "chat_id": cid,
                                "invite_url": str(val.get("invite_url") or val.get("url") or "").strip(),
                            }
                    else:
                        cid = str(val).strip()
                        if cid:
                            extra_channels[alias_clean] = {
                                "name": alias_clean.upper(),
                                "chat_id": cid,
                                "invite_url": "",
                            }
        except Exception:
            pass

    for idx in range(2, 11):
        for fmt in (f"{idx}", f"{idx:02d}", f"{idx:03d}"):
            cid = env_map.get(f"PS2N_TG_CHANNEL_{fmt}_ID", "").strip()
            if cid:
                alias = env_map.get(f"PS2N_TG_CHANNEL_{fmt}_ALIAS", f"channel_{idx:02d}").strip().lower()
                cname = env_map.get(f"PS2N_TG_CHANNEL_{fmt}_NAME", f"Channel #{idx}").strip()
                curl = env_map.get(f"PS2N_TG_CHANNEL_{fmt}_URL", "").strip()
                extra_channels[alias] = {
                    "name": cname,
                    "chat_id": cid,
                    "invite_url": curl,
                }
                break

    return extra_channels


class TelegramConfigManager:
    """
    Pengurus tunggal konfigurasi Telegram (Singleton) yang menyatukan pembacaan
    dari GitHub Secret `TELEGRAM_ALL_KEY_JSON` dan fail tempatan `.env.local`.
    """

    def __init__(self, env_path: Path = DEFAULT_ENV_LOCAL_PATH) -> None:
        self.env_path = env_path
        self.source_mode: str = "UNINITIALIZED"

        # 1. Maklumat Bot
        self.bot_id: str = ""
        self.bot_token: str = ""
        self.bot_link: str = ""

        # 2. Maklumat DM Admin & Multi-Channel
        self.admin_chat_id: str = ""
        self.default_channel_id: str = ""
        self.default_channel_url: str = ""
        self.default_channel_name: str = "PS2 Retro Nostalgia FB (Utama)"
        self.channels: Dict[str, Dict[str, str]] = {}

        # 3. Maklumat Aplikasi API Telegram (my.telegram.org)
        self.api_id: int = 0
        self.api_hash: str = ""
        self.app_title: str = ""
        self.short_name: str = ""

        # 4. Konfigurasi Pelayan MTProto Test & Production
        self.test_configuration: str = ""
        self.test_public_key: str = ""
        self.production_configuration: str = ""
        self.production_public_keys: str = ""

        self.reload()

    def reload(self) -> None:
        """Memuat semula semua kunci daripada GitHub Secret JSON dan/atau .env.local."""
        merged_env: Dict[str, str] = {}

        # Baca .env.local dahulu jika wujud (untuk WSL tempatan, termasuk kunci RSA berbilang baris)
        if self.env_path.exists():
            load_dotenv(dotenv_path=self.env_path, override=False)
            raw_file_vars = dotenv_values(self.env_path)
            for k, v in raw_file_vars.items():
                if k and v is not None:
                    merged_env[k.strip()] = str(v).strip()

        # Pemboleh ubah persekitaran OS sentiasa diberi keutamaan tertinggi
        for k, v in os.environ.items():
            if k.startswith("PS2N_TG_") or k == "TELEGRAM_ALL_KEY_JSON":
                merged_env[k] = v.strip()

        gh_json_raw = merged_env.get("TELEGRAM_ALL_KEY_JSON", "").strip()
        loaded_from_json = False

        if gh_json_raw:
            try:
                bundle = json.loads(gh_json_raw)
                if isinstance(bundle, dict):
                    self._apply_json_bundle(bundle)
                    loaded_from_json = True
                    self.source_mode = "GITHUB_SECRET_JSON"
            except Exception as exc:
                raise RuntimeError(f"Format JSON dalam TELEGRAM_ALL_KEY_JSON tidak sah: {exc}") from exc

        # Jika tidak dimuatkan dari JSON (mod WSL tempatan) atau ada override PS2N_TG_*, baca kunci individu
        if not loaded_from_json:
            self.source_mode = "LOCAL_ENV_FILE" if self.env_path.exists() else "OS_ENV_VARS"

        self._apply_individual_env_overrides(merged_env)

    def _apply_json_bundle(self, bundle: Dict[str, Any]) -> None:
        bot_sec = bundle.get("bot", {})
        if isinstance(bot_sec, dict):
            self.bot_id = str(bot_sec.get("bot_id", "")).strip()
            self.bot_token = str(bot_sec.get("bot_token", "")).strip()
            self.bot_link = str(bot_sec.get("bot_link", "")).strip()

        dm_sec = bundle.get("admin_dm", {})
        if isinstance(dm_sec, dict):
            self.admin_chat_id = str(dm_sec.get("chat_id", "")).strip()

        ch_sec = bundle.get("channels", {})
        if isinstance(ch_sec, dict):
            for alias, ch_info in ch_sec.items():
                if isinstance(ch_info, dict) and ch_info.get("chat_id"):
                    self.channels[str(alias).lower()] = {
                        "name": str(ch_info.get("name", alias)),
                        "chat_id": str(ch_info.get("chat_id", "")).strip(),
                        "invite_url": str(ch_info.get("invite_url", "")).strip(),
                    }
            if "default" in self.channels:
                self.default_channel_id = self.channels["default"]["chat_id"]
                self.default_channel_url = self.channels["default"].get("invite_url", "")
                self.default_channel_name = self.channels["default"].get("name", self.default_channel_name)

        app_sec = bundle.get("api_app", {})
        if isinstance(app_sec, dict):
            raw_id = str(app_sec.get("api_id", "")).strip()
            if raw_id.isdigit():
                self.api_id = int(raw_id)
            self.api_hash = str(app_sec.get("api_hash", "")).strip()
            self.app_title = str(app_sec.get("app_title", "")).strip()
            self.short_name = str(app_sec.get("short_name", "")).strip()

        mtp_sec = bundle.get("mtproto_servers", {})
        if isinstance(mtp_sec, dict):
            self.test_configuration = str(mtp_sec.get("test_configuration", "")).strip()
            self.test_public_key = str(mtp_sec.get("test_public_key", "")).strip()
            self.production_configuration = str(mtp_sec.get("production_configuration", "")).strip()
            self.production_public_keys = str(mtp_sec.get("production_public_keys", "")).strip()

    def _apply_individual_env_overrides(self, env_map: Dict[str, str]) -> None:
        if env_map.get("PS2N_TG_BOT_TOKEN"):
            self.bot_token = env_map["PS2N_TG_BOT_TOKEN"]
        if env_map.get("PS2N_TG_BOT_ID"):
            self.bot_id = env_map["PS2N_TG_BOT_ID"]
        elif not self.bot_id and ":" in self.bot_token:
            self.bot_id = self.bot_token.split(":", 1)[0]
        if env_map.get("PS2N_TG_BOT_LINK"):
            self.bot_link = env_map["PS2N_TG_BOT_LINK"]

        if env_map.get("PS2N_TG_CHAT_ID"):
            self.admin_chat_id = env_map["PS2N_TG_CHAT_ID"]

        if env_map.get("PS2N_TG_CHANNEL_ID"):
            self.default_channel_id = env_map["PS2N_TG_CHANNEL_ID"]
        if env_map.get("PS2N_TG_CHANNEL_URL"):
            self.default_channel_url = env_map["PS2N_TG_CHANNEL_URL"]

        if self.default_channel_id:
            self.channels["default"] = {
                "name": self.default_channel_name,
                "chat_id": self.default_channel_id,
                "invite_url": self.default_channel_url,
            }

        extra_chs = _parse_extra_channels_from_env(env_map)
        for alias, ch_data in extra_chs.items():
            self.channels[alias] = ch_data

        raw_api_id = env_map.get("PS2N_TG_APP_API_ID", "").strip()
        if raw_api_id.isdigit():
            self.api_id = int(raw_api_id)
        if env_map.get("PS2N_TG_APP_API_HASH"):
            self.api_hash = env_map["PS2N_TG_APP_API_HASH"]
        if env_map.get("PS2N_TG_APP_TITLE"):
            self.app_title = env_map["PS2N_TG_APP_TITLE"]
        if env_map.get("PS2N_TG_SHORT_NAME"):
            self.short_name = env_map["PS2N_TG_SHORT_NAME"]

        if env_map.get("PS2N_TG_TEST_CONFIGURATION"):
            self.test_configuration = env_map["PS2N_TG_TEST_CONFIGURATION"]
        if env_map.get("PS2N_TG_TEST_PUBLIC_KEY"):
            self.test_public_key = env_map["PS2N_TG_TEST_PUBLIC_KEY"]
        if env_map.get("PS2N_TG_PRODUCTION_CONFIGURATION"):
            self.production_configuration = env_map["PS2N_TG_PRODUCTION_CONFIGURATION"]
        if env_map.get("PS2N_TG_PRODUCTION_PUBLIC_KEYS"):
            self.production_public_keys = env_map["PS2N_TG_PRODUCTION_PUBLIC_KEYS"]

    @property
    def bot_api_base_url(self) -> str:
        """URL asas rasmi Telegram Bot API bagi bot yang dikonfigurasikan."""
        if not self.bot_token:
            return ""
        return f"https://api.telegram.org/bot{self.bot_token}"

    def get_channel_id(self, alias: str = "default", fallback_to_default: bool = True) -> str:
        """
        Mendapatkan chat_id channel berdasarkan alias (contoh: 'default', 'fb', 'error')
        atau terus memulangkan nilai jika ID berawalan '-100' diberikan.
        """
        key = (alias or "default").strip().lower()
        if key.startswith("-100") and key[4:].isdigit():
            return key
        if key in self.channels:
            return self.channels[key]["chat_id"]
        if fallback_to_default:
            return self.default_channel_id
        return ""

    def list_channels(self) -> Dict[str, Dict[str, str]]:
        """Memulangkan salinan daftar semua channel berdaftar di bawah bot ini."""
        return {k: dict(v) for k, v in self.channels.items()}

    def validate(self) -> Tuple[bool, List[str]]:
        """
        Mengesahkan kelengkapan kunci utama Telegram sebelum penghantaran dilakukan.
        Mengembalikan (status_sah, senarai_ralat).
        """
        errors: List[str] = []
        if not self.bot_token or ":" not in self.bot_token:
            errors.append("PS2N_TG_BOT_TOKEN tiada atau tidak sah.")
        if not self.admin_chat_id:
            errors.append("PS2N_TG_CHAT_ID (DM Admin) tiada.")
        if not self.default_channel_id or not self.default_channel_id.startswith("-100"):
            errors.append("PS2N_TG_CHANNEL_ID tiada atau tidak bermula dengan -100.")
        if self.api_id <= 0:
            errors.append("PS2N_TG_APP_API_ID tiada atau tidak sah.")
        if not self.api_hash or len(self.api_hash) != 32:
            errors.append("PS2N_TG_APP_API_HASH tiada atau panjang bukan 32 aksara.")
        return len(errors) == 0, errors

    def is_configured(self) -> bool:
        ok, _ = self.validate()
        return ok

    def get_masked_summary(self) -> Dict[str, Any]:
        """Ringkasan bertopeng yang selamat untuk dipaparkan di log terminal/CI."""
        tok_masked = (
            f"{self.bot_token[:6]}...{self.bot_token[-6:]}"
            if len(self.bot_token) > 14
            else ("ADA" if self.bot_token else "TIADA")
        )
        hash_masked = (
            f"{self.api_hash[:6]}...{self.api_hash[-6:]}"
            if len(self.api_hash) > 14
            else ("ADA" if self.api_hash else "TIADA")
        )
        return {
            "source_mode": self.source_mode,
            "bot_id": self.bot_id,
            "bot_link": self.bot_link,
            "bot_token": tok_masked,
            "admin_chat_id": self.admin_chat_id,
            "default_channel_id": self.default_channel_id,
            "registered_channels_count": len(self.channels),
            "channel_aliases": list(self.channels.keys()),
            "api_id": self.api_id,
            "api_hash": hash_masked,
            "app_title": self.app_title,
            "short_name": self.short_name,
            "has_test_rsa": "BEGIN RSA PUBLIC KEY" in self.test_public_key,
            "has_prod_rsa": "BEGIN RSA PUBLIC KEY" in self.production_public_keys,
        }


# Instans Singleton utama untuk diimport oleh semua modul lain
tg_config = TelegramConfigManager()


if __name__ == "__main__":
    is_ok, errs = tg_config.validate()
    print("Status Pengesahan X10_telegram_config:", is_ok)
    if errs:
        print("Ralat:", errs)
    print(json.dumps(tg_config.get_masked_summary(), indent=2, ensure_ascii=False))