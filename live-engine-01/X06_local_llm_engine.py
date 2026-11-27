#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - ENJIN TERAS LLM TEMPATAN & GITHUB ACTIONS (X06)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X06_local_llm_engine.py
#
# FUNGSI UTAMA:
# 1. Klien Singleton berpusat untuk model GGUF Qwen3.5-4B-Q4_K_M.gguf.
# 2. Ditala khusus untuk Bahasa Melayu Malaysia yang santai, bersahaja, dan
#    tidak kaku seperti terjemahan mesin (repeat_penalty=1.08, temp=0.64).
# 3. Menyuntik panduan gaya bahasa Malaysia semula jadi secara automatik ke
#    dalam semua panggilan cerita (X11, X13, dan modul masa hadapan).
# 4. Pembersih output AI: buang tag <think>, buang tajuk/header, buang emoji AI,
#    tukar istilah Indonesia & frasa terjemahan janggal ke BM tempatan, dan
#    potong kemas pada penghujung noktah ayat.
# ==============================================================================

import gc
import importlib
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
# [BAHAGIAN TUNING MANUAL] - DITALA UNTUK BM SANTAI & SEMULA JADI
# ==============================================================================
# 1. Kawalan Kreativiti & Kestabilan Bahasa Melayu (Elak Perkataan Pelik)
DEFAULT_TEMPERATURE: float = float(os.getenv("PS2N_LLM_TEMP", "0.64"))
DEFAULT_MAX_TOKENS: int = int(os.getenv("PS2N_LLM_MAX_TOKENS", "768"))
DEFAULT_TOP_P: float = float(os.getenv("PS2N_LLM_TOP_P", "0.88"))
DEFAULT_TOP_K: int = int(os.getenv("PS2N_LLM_TOP_K", "30"))
DEFAULT_REPEAT_PENALTY: float = float(os.getenv("PS2N_LLM_REPEAT_PENALTY", "1.08"))
DEFAULT_PRESENCE_PENALTY: float = float(os.getenv("PS2N_LLM_PRESENCE_PENALTY", "0.05"))
DEFAULT_FREQUENCY_PENALTY: float = float(os.getenv("PS2N_LLM_FREQUENCY_PENALTY", "0.05"))

# 2. Kawalan Perkakasan & Memori (WSL 4 Threads / GitHub Actions Ubuntu Runner)
DEFAULT_N_CTX: int = int(os.getenv("PS2N_LLM_N_CTX", "4096"))
DEFAULT_N_THREADS: int = int(os.getenv("PS2N_LLM_N_THREADS", str(min(4, max(3, os.cpu_count() or 4)))))
DEFAULT_N_BATCH: int = int(os.getenv("PS2N_LLM_N_BATCH", "512"))
DEFAULT_KV_CACHE_TYPE: int = int(os.getenv("PS2N_LLM_KV_CACHE_TYPE", "8"))  # 8 = Q8_0 (jimat RAM)
DEFAULT_FLASH_ATTN: bool = os.getenv("PS2N_LLM_FLASH_ATTN", "true").lower() in ("1", "true", "yes")

# 3. Kawalan Mod Penaakulan Qwen3.5 & Kualiti Cerita Bahasa Melayu
ENABLE_THINKING: bool = os.getenv("PS2N_LLM_ENABLE_THINKING", "false").lower() in ("1", "true", "yes")
DEFAULT_MIN_STORY_CHARS: int = int(os.getenv("PS2N_MIN_STORY_CHARS", "220"))
DEFAULT_MAX_STORY_CHARS: int = int(os.getenv("PS2N_MAX_STORY_CHARS", "1150"))
DEFAULT_SOFT_TRIM_CHARS: int = int(os.getenv("PS2N_SOFT_TRIM_CHARS", "1100"))

# 4. Rujukan Model Fallback Hugging Face (Untuk GitHub Actions)
HF_DEFAULT_REPO_ID: str = "unsloth/Qwen3.5-4B-GGUF"
HF_DEFAULT_MODEL_FILE: str = "Qwen3.5-4B-Q4_K_M.gguf"
# ==============================================================================


# Panduan gaya bahasa Malaysia semula jadi yang disuntik automatik ke dalam system prompt
NATURAL_MALAY_STYLE_DIRECTIVE: str = (
    "\n\n[PANDUAN GAYA BAHASA MALAYSIA WAJIB]:\n"
    "- Fikir dan tulis terus dalam Bahasa Melayu Malaysia yang santai macam abang-abang bersembang di kedai kopi.\n"
    "- JANGAN terjemah ayat sumber bahasa asing (English/Perancis/dll.) perkataan demi perkataan; "
    "ambil maksudnya sahaja dan ceritakan semula guna ayat sendiri yang ringkas dan sedap dibaca.\n"
    "- Gunakan ayat aktif, terus terang, dan mudah difahami (elakkan struktur ayat berbunga-bunga seperti "
    "'Ini bukan sekadar X, tapi kita sedang mengembalikan...' atau 'benda mati dalam lemari')."
)

# Kamus penyingkiran istilah Indonesia -> Bahasa Melayu harian Malaysia
INDONESIAN_SCRUB_MAP: Dict[str, str] = {
    r"\bbisa\b": "boleh",
    r"\bbanget\b": "sangat",
    r"\bnggak\b": "tak",
    r"\bngak\b": "tak",
    r"\bgampang\b": "senang",
    r"\bbikin\b": "buat",
    r"\bcobain\b": "cuba",
    r"\bcocok\b": "sesuai",
    r"\byuk\b": "jom",
    r"\bgimana\b": "macam mana",
    r"\bkulkas\b": "peti sejuk",
    r"\bberantakan\b": "berselerak",
    r"\bcapek\b": "penat",
    r"\buang\b": "duit",
    r"\blemari\b": "almari",
    r"\bkayak\b": "macam",
    r"\bdoang\b": "sahaja",
    r"\budah\b": "dah",
    r"\baja\b": "je",
    r"\bkalo\b": "kalau",
    r"\bpake\b": "guna",
    r"\bAnda\b": "korang",
    r"\banda\b": "korang",
    r"\bkalian\b": "korang",
    r"\bteman-teman\b": "kawan-kawan",
    r"\bnggak\s+usah\b": "tak payah",
}

# Pembaikan frasa terjemahan literal kaku & typo biasa model 4B
AWKWARD_PHRASE_SCRUB_MAP: Dict[str, str] = {
    r"\bsereis\b": "siri",
    r"\bkesedih\b": "sedih",
    r"\bbenda mati\b": "tersadai",
    r"\bkonsol masa emas\b": "konsol legenda",
    r"\bpakej keselamatan\b": "sistem sekuriti",
    r"\bdicantum semasa produksi awal\b": "dipasang terus dari kilang",
    r"\bteknologi serba digital hari ini\b": "cara moden hari ni",
    r"\bmenyebabkan keputusasaan\b": "membuatkan ramai buntu",
}


def resolve_text_model_file() -> Tuple[str, str]:
    """
    Mengesan lokasi fail GGUF teks sama ada dari folder tempatan WSL
    atau memuat turun secara automatik melalui cache Hugging Face Hub (GitHub Actions).
    Mengembalikan (laluan_fail_gguf, sumber_mod).
    """
    repo_id = HF_DEFAULT_REPO_ID
    filename = HF_DEFAULT_MODEL_FILE
    local_candidate: Optional[Path] = None

    if cfg is not None and hasattr(cfg, "resolve_gguf_paths"):
        info = cfg.resolve_gguf_paths()
        repo_id = info.get("repo_id", repo_id)
        if info.get("model_path"):
            local_candidate = Path(info["model_path"])
            filename = local_candidate.name or filename

    if local_candidate is None:
        local_dir = Path(os.getenv("PS2N_LOCAL_GGUF_DIR", "/home/braderdin/X-HF-model-gguf"))
        local_candidate = local_dir / filename

    if local_candidate.exists() and local_candidate.stat().st_size > 100 * 1024 * 1024:
        return str(local_candidate), "LOCAL_DISK"

    hf_hub: Any = importlib.import_module("huggingface_hub")
    hf_hub_download: Any = getattr(hf_hub, "hf_hub_download")
    cached_path = hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        token=os.getenv("HF_TOKEN") or None,
    )
    return str(cached_path), "HF_HUB_CACHE"


def trim_to_sentence_boundary(text: str, max_chars: int = DEFAULT_SOFT_TRIM_CHARS, min_keep: int = 200) -> str:
    """
    Memotong teks secara kemas pada penghujung ayat lengkap (., !, ?) yang terakhir
    di bawah had max_chars supaya cerita tidak terputus di tengah ayat.
    """
    if len(text) <= max_chars:
        return text

    sliced = text[:max_chars]
    last_punct = max(sliced.rfind("."), sliced.rfind("!"), sliced.rfind("?"))
    if last_punct >= min_keep:
        return sliced[: last_punct + 1].strip()
    return sliced.strip()


def clean_and_scrub_bm_story(
    raw_text: str,
    max_chars: int = DEFAULT_SOFT_TRIM_CHARS,
    scrub_indonesian: bool = True,
    strip_emojis: bool = True,
) -> str:
    """
    Membersihkan output mentah Qwen3.5-4B:
    1. Membuang blok <think>...</think> dan pembungkus markdown.
    2. Menormalkan tanda petik dan sengkang.
    3. Membuang emoji mentah AI dan baris tajuk/header di baris pertama.
    4. Menggantikan perkataan Indonesia & frasa terjemahan kaku ke BM Malaysia.
    5. Memastikan ayat berakhir pada noktah lengkap di bawah had aksara.
    """
    if not raw_text:
        return ""

    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw_text, flags=re.DOTALL)
    cleaned = re.sub(r"```[a-zA-Z]*\s*|```\s*", "", cleaned)

    for orig, rep in {"’": "'", "‘": "'", "“": '"', "”": '"', "—": "-", "–": "-", "…": "..."}.items():
        cleaned = cleaned.replace(orig, rep)

    if strip_emojis:
        emoji_pattern = re.compile(
            "[\U00010000-\U0010ffff\uD800-\uDBFF\uDC00-\uDFFF\u2600-\u26FF\u2700-\u27BF]",
            flags=re.UNICODE,
        )
        cleaned = emoji_pattern.sub("", cleaned)

    cleaned = cleaned.strip().strip('"').strip("'")

    # Buang baris pertama jika AI cuba meletakkan tajuk/header
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    if lines and (
        lines[0].lower().startswith(("tajuk", "title", "headline", "kapsyen", "caption"))
        or lines[0].startswith(("#", "**"))
    ):
        lines.pop(0)
    cleaned = " ".join(lines)

    if scrub_indonesian:
        for pattern, bm_word in INDONESIAN_SCRUB_MAP.items():
            cleaned = re.sub(pattern, bm_word, cleaned, flags=re.IGNORECASE)
        for pattern, natural_bm in AWKWARD_PHRASE_SCRUB_MAP.items():
            cleaned = re.sub(pattern, natural_bm, cleaned, flags=re.IGNORECASE)

    # Pastikan berhenti pada tanda noktah/seruan ayat lengkap
    match = re.search(r"^([\s\S]*[.!?])", cleaned.strip())
    if match:
        cleaned = match.group(1).strip()

    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if max_chars > 0:
        cleaned = trim_to_sentence_boundary(cleaned, max_chars=max_chars)
    return cleaned


def validate_story_quality(
    text: str,
    min_len: int = DEFAULT_MIN_STORY_CHARS,
    max_len: int = DEFAULT_MAX_STORY_CHARS,
) -> Tuple[bool, str]:
    """
    Menyemak kualiti kapsyen cerita supaya bebas daripada glitch pengulangan,
    aksara luar biasa, atau panjang yang tidak menepati had.
    """
    if not text or len(text) < min_len:
        return False, f"Teks terlalu pendek ({len(text)} aksara, minimum {min_len})."
    if len(text) > max_len:
        return False, f"Teks melebihi had ({len(text)} aksara, maksimum {max_len})."

    allowed_re = re.compile(r"^[a-zA-Z0-9\s.,!?'\"\-\(\)/%:;&]+$")
    if not allowed_re.match(text):
        return False, "Dikesan aksara atau simbol tidak standard di luar abjad Latin."

    words = re.findall(r"\b\w+\b", text.lower())
    freq: Dict[str, int] = {}
    for w in words:
        if len(w) > 3:
            freq[w] = freq.get(w, 0) + 1
            if freq[w] > 10:
                return False, f"Glitch pengulangan: perkataan '{w}' muncul {freq[w]} kali."

    return True, "Lulus"


class LocalLLMEngine:
    """
    Klien Singleton terpusat untuk inferens teks Qwen3.5-4B-Q4_K_M.gguf.
    Boleh digunakan secara tempatan di WSL atau di GitHub Actions.
    """

    _instance: Optional["LocalLLMEngine"] = None

    def __new__(cls) -> "LocalLLMEngine":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._llm = None
            cls._instance._model_path = ""
            cls._instance._source_mode = ""
            cls._instance._load_seconds = 0.0
        return cls._instance

    def get_tuning_profile(self) -> Dict[str, Any]:
        return {
            "temperature": DEFAULT_TEMPERATURE,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "top_p": DEFAULT_TOP_P,
            "top_k": DEFAULT_TOP_K,
            "repeat_penalty": DEFAULT_REPEAT_PENALTY,
            "presence_penalty": DEFAULT_PRESENCE_PENALTY,
            "frequency_penalty": DEFAULT_FREQUENCY_PENALTY,
            "n_ctx": DEFAULT_N_CTX,
            "n_threads": DEFAULT_N_THREADS,
            "n_batch": DEFAULT_N_BATCH,
            "kv_cache_type": DEFAULT_KV_CACHE_TYPE,
            "flash_attn": DEFAULT_FLASH_ATTN,
            "enable_thinking": ENABLE_THINKING,
            "min_story_chars": DEFAULT_MIN_STORY_CHARS,
            "max_story_chars": DEFAULT_MAX_STORY_CHARS,
            "soft_trim_chars": DEFAULT_SOFT_TRIM_CHARS,
        }

    def ensure_loaded(
        self,
        n_ctx: int = DEFAULT_N_CTX,
        n_threads: int = DEFAULT_N_THREADS,
        n_batch: int = DEFAULT_N_BATCH,
        kv_cache_type: int = DEFAULT_KV_CACHE_TYPE,
        flash_attn: bool = DEFAULT_FLASH_ATTN,
    ) -> Any:
        if self._llm is not None:
            return self._llm

        model_path, source_mode = resolve_text_model_file()
        llama_mod: Any = importlib.import_module("llama_cpp")
        Llama: Any = getattr(llama_mod, "Llama")

        t0 = time.perf_counter()
        try:
            self._llm = Llama(
                model_path=model_path,
                n_ctx=n_ctx,
                n_threads=n_threads,
                n_batch=n_batch,
                type_k=kv_cache_type,
                type_v=kv_cache_type,
                flash_attn=flash_attn,
                verbose=False,
            )
        except TypeError:
            self._llm = Llama(
                model_path=model_path,
                n_ctx=n_ctx,
                n_threads=n_threads,
                n_batch=n_batch,
                verbose=False,
            )

        self._load_seconds = round(time.perf_counter() - t0, 2)
        self._model_path = model_path
        self._source_mode = source_mode
        return self._llm

    def unload(self) -> None:
        """Membebaskan model daripada RAM (penting sebelum/selepas memuatkan enjin Vision)."""
        if self._llm is not None:
            del self._llm
            self._llm = None
            gc.collect()

    def generate_chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        top_p: float = DEFAULT_TOP_P,
        top_k: int = DEFAULT_TOP_K,
        repeat_penalty: float = DEFAULT_REPEAT_PENALTY,
        presence_penalty: float = DEFAULT_PRESENCE_PENALTY,
        frequency_penalty: float = DEFAULT_FREQUENCY_PENALTY,
        enable_thinking: bool = ENABLE_THINKING,
        enforce_natural_bm: bool = True,
    ) -> Dict[str, Any]:
        """
        Menjalankan satu panggilan inferens chat dan mengembalikan hasil mentah
        serta metrik prestasi (masa, token, tok/s).
        """
        llm = self.ensure_loaded()

        final_system_prompt = system_prompt.strip()
        if (
            enforce_natural_bm
            and "json" not in final_system_prompt.lower()
            and "[PANDUAN GAYA BAHASA MALAYSIA WAJIB]" not in final_system_prompt
        ):
            final_system_prompt = f"{final_system_prompt}{NATURAL_MALAY_STYLE_DIRECTIVE}"

        final_user_prompt = user_prompt.strip()
        if not enable_thinking and not final_user_prompt.endswith("/no_think"):
            final_user_prompt = f"{final_user_prompt}\n/no_think"

        t0 = time.perf_counter()
        res: Any = llm.create_chat_completion(
            messages=[
                {"role": "system", "content": final_system_prompt},
                {"role": "user", "content": final_user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            top_k=top_k,
            repeat_penalty=repeat_penalty,
            presence_penalty=presence_penalty,
            frequency_penalty=frequency_penalty,
            stream=False,
        )
        elapsed = time.perf_counter() - t0

        raw_text = str(res["choices"][0]["message"]["content"] or "")
        usage = res.get("usage", {})
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", max(1, len(raw_text.split()))))
        tok_per_sec = round(completion_tokens / elapsed, 2) if elapsed > 0 else 0.0

        return {
            "raw_text": raw_text,
            "elapsed_sec": round(elapsed, 2),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "tok_per_sec": tok_per_sec,
            "source_mode": self._source_mode,
            "load_sec": self._load_seconds,
        }

    def generate_bm_story(
        self,
        system_prompt: str,
        user_prompt: str,
        max_attempts: int = 3,
        min_chars: int = DEFAULT_MIN_STORY_CHARS,
        max_chars: int = DEFAULT_MAX_STORY_CHARS,
        soft_trim_chars: int = DEFAULT_SOFT_TRIM_CHARS,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Menjana cerita nostalgia Bahasa Melayu Malaysia dengan gelung cubaan semula
        automatik, pembersihan istilah Indonesia, dan semakan kualiti.
        """
        last_meta: Dict[str, Any] = {}

        for attempt in range(1, max_attempts + 1):
            adj_temp = min(0.88, round(temperature + ((attempt - 1) * 0.05), 2))
            inf = self.generate_chat(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=adj_temp,
                max_tokens=max_tokens,
            )
            cleaned = clean_and_scrub_bm_story(inf["raw_text"], max_chars=soft_trim_chars)
            ok_qual, qual_msg = validate_story_quality(cleaned, min_len=min_chars, max_len=max_chars)

            last_meta = {
                "attempt": attempt,
                "temperature_used": adj_temp,
                "elapsed_sec": inf["elapsed_sec"],
                "completion_tokens": inf["completion_tokens"],
                "tok_per_sec": inf["tok_per_sec"],
                "story_chars": len(cleaned),
                "quality_status": qual_msg,
                "source_mode": inf["source_mode"],
            }

            if ok_qual:
                return True, cleaned, last_meta

        return False, "", last_meta


llm_engine = LocalLLMEngine()


if __name__ == "__main__":
    print("Profil Tuning X06:", llm_engine.get_tuning_profile())
    ok, story, meta = llm_engine.generate_bm_story(
        system_prompt=(
            "Anda ialah pengendali halaman 'PS2 Retro Nostalgia' di Malaysia. "
            "Tulis 1 perenggan pendek kenangan bermain PS2 dalam Bahasa Melayu Malaysia yang santai. "
            "Dilarang tajuk, emoji, atau perkataan Indonesia."
        ),
        user_prompt="Tulis kenangan bermain PS2 waktu malam minggu bersama kawan-kawan (250-550 aksara).",
        max_tokens=320,
    )
    print("Status:", ok, "| Meta:", meta)
    print("Hasil:", story)
    llm_engine.unload()