"""This computer's hardware, the speech model that suits it, and the model names
shown everywhere in Gusan 2.0. No Qt: pages, onboarding and tests use it as is.

What is here
    snapshot(cfg)          CPU name, physical cores, RAM (raw + rounded for display),
                           GPU name and gpu_accel.status(cfg), plus the recommendation.
                           SLOW the first time (PowerShell CIM up to 6 s, ctranslate2
                           import ~0.3 s): always call it on a worker thread
                           (model_manager.ModelDownloads.probe_hardware does that).
                           The CPU name and GPU names are cached for the run
                           (an empty answer is not: the next snapshot asks again).
    recommend_model(...)   the onboarding / Settings "Best for your computer" rule.
    MODEL_INFO             speech model ids -> plain name, real name, size, memory.
    LOCAL_AI_INFO          local AI writer ids (local_llm) -> the same fields.
    CLOUD_*_MODEL          the Gusan Cloud model names.
    cpu_name(), detect_gpu()   lifted from ui/settings.py (Settings._cpu_name /
                           _detect_gpu); the old dialog keeps its own copies until it
                           is retired.

The recommendation rule (BRIEF section 5; Mac never uses the GPU; a GPU in state
'failed' or 'unsupported' falls back to the CPU rules):
    NVIDIA GPU ready/available + ~8 GB  -> large-v3-turbo ("Most accurate")
    ~8 GB + 6 physical cores            -> small ("Balanced")
    ~4 GB                               -> base ("Fast")
    else                                -> tiny ("Lightest") + suggest Gusan Cloud
"~8 GB" compares the RAM rounded to its nominal size (display_ram_gb), so the
7.4-7.9 GB an 8 GB laptop reports still counts as 8 - and the chip the user sees
("8 GB memory") always agrees with the rule.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from typing import Optional

logger = logging.getLogger("transcribe")


# ── Model names (BRIEF section 0, rule 1) ─────────────────────────────────────
@dataclass(frozen=True)
class ModelInfo:
    id: str             # the config / download id ("small", "qwen_tiny")
    kind: str           # "speech" | "writer"
    plain: str          # what the UI leads with ("Balanced")
    real: str           # the real model, shown small under it ("Whisper Small")
    size: str           # download size as shown ("460 MB")
    size_mb: int        # the same in MB, for sums and comparisons
    memory_gb: int      # memory it needs ("4 GB memory")
    more_sizes: bool = False   # speech: listed under "More sizes"
    speed: int = 0      # speech: 1-5 bars on the model cards (5 = fastest)
    accuracy: int = 0   # speech: 1-5 bars (5 = most accurate)
    speed_rank: int = 0  # speech: main.MODELS speed_rank (1 = fastest), for speed_phrase

    def label(self, with_size=False):
        """'Balanced · Whisper Small' (+ ' · 460 MB')."""
        text = "%s · %s" % (self.plain, self.real)
        return "%s · %s" % (text, self.size) if with_size else text


SPEECH = "speech"
WRITER = "writer"

# Order = the order the UI lists them. Sizes and memory match main.MODELS
# (tests/test_hardware.py checks it) and the mockups (gusan_ds.SPEECH_MODELS).
MODEL_INFO = {
    "tiny": ModelInfo("tiny", SPEECH, "Lightest", "Whisper Tiny", "75 MB", 75, 2,
                      more_sizes=True, speed=5, accuracy=2, speed_rank=1),
    "base": ModelInfo("base", SPEECH, "Fast", "Whisper Base", "140 MB", 140, 2,
                      speed=5, accuracy=3, speed_rank=2),
    "small": ModelInfo("small", SPEECH, "Balanced", "Whisper Small", "460 MB", 460, 4,
                       speed=4, accuracy=4, speed_rank=3),
    "medium": ModelInfo("medium", SPEECH, "Accurate", "Whisper Medium", "1.4 GB", 1400, 6,
                        more_sizes=True, speed=2, accuracy=4, speed_rank=5),
    "large-v3-turbo": ModelInfo("large-v3-turbo", SPEECH, "Most accurate", "Whisper Large v3 Turbo",
                                "1.6 GB", 1600, 6, speed=3, accuracy=5, speed_rank=4),
    "large-v3": ModelInfo("large-v3", SPEECH, "Maximum", "Whisper Large v3", "3 GB", 3000, 8,
                          more_sizes=True, speed=1, accuracy=5, speed_rank=6),
}
SPEECH_MODEL_IDS = tuple(MODEL_INFO)
MAIN_SPEECH_MODELS = tuple(m for m in SPEECH_MODEL_IDS if not MODEL_INFO[m].more_sizes)   # the 3 big cards
MORE_SPEECH_MODELS = tuple(m for m in SPEECH_MODEL_IDS if MODEL_INFO[m].more_sizes)       # "More sizes"

# Local AI writer (Smart Actions, meeting notes, Live Assistant on this computer).
# Ids are local_llm's; sizes and memory match local_llm.MODEL_CATALOG.
LOCAL_AI_INFO = {
    "qwen_tiny": ModelInfo("qwen_tiny", WRITER, "Small", "Qwen 1.5B", "1.12 GB", 1120, 8),
    "qwen_3b": ModelInfo("qwen_3b", WRITER, "Medium", "Qwen 3B", "2.3 GB", 2300, 12),
    "qwen_7b": ModelInfo("qwen_7b", WRITER, "Large", "Qwen 7B", "4.7 GB", 4700, 16),
    "gemma_2b": ModelInfo("gemma_2b", WRITER, "Alternative", "Gemma 2 2B", "1.6 GB", 1600, 8),
}
LOCAL_AI_MODEL_IDS = tuple(LOCAL_AI_INFO)
# 1.9.x ids still found in old configs (local_llm.LEGACY_MODEL_IDS).
LEGACY_LOCAL_AI_IDS = {"aibuben_tiny": "qwen_tiny", "aibuben_balanced": "qwen_3b", "aibuben_gpu": "qwen_7b"}

# Gusan Cloud (Pro). Names only - the routing lives in main / engine_choice.
CLOUD_SPEECH_MODEL = "Gemini 2.5 Flash"     # cloud speech (transcribe-proxy default)
CLOUD_WRITER_MODEL = "Mistral Small"        # meeting notes (smart-action tries Claude Sonnet 5.5 first once its key is scoped to a workspace)
CLOUD_LIVE_MODEL = "DeepSeek V4.1 Flash"    # Live Assistant answers
CLOUD_SMART_MODEL = CLOUD_LIVE_MODEL        # Smart Actions (smart-action SMART_MODES)

DEFAULT_SPEECH_MODEL = "base"               # main.DEFAULT["whisper_model"]


def info(model_id) -> Optional[ModelInfo]:
    """The ModelInfo for a speech or local AI id (legacy ids too), else None."""
    mid = LEGACY_LOCAL_AI_IDS.get(model_id, model_id)
    return MODEL_INFO.get(mid) or LOCAL_AI_INFO.get(mid)


def plain_name(model_id):
    """'small' -> 'Balanced'. Unknown ids come back as they are."""
    i = info(model_id)
    return i.plain if i else str(model_id or "")


def real_name(model_id):
    """'small' -> 'Whisper Small', 'qwen_tiny' -> 'Qwen 1.5B'."""
    i = info(model_id)
    return i.real if i else str(model_id or "")


def model_label(model_id, with_size=False):
    """'Balanced · Whisper Small' (+ ' · 460 MB'); the id itself when unknown."""
    i = info(model_id)
    return i.label(with_size) if i else str(model_id or "")


# ── RAM ───────────────────────────────────────────────────────────────────────
# Sizes RAM is sold in (GB). 18/36 are Apple's.
_NOMINAL_GB = (1, 2, 3, 4, 6, 8, 12, 16, 18, 24, 32, 36, 48, 64, 96, 128, 192, 256, 384, 512)


def display_ram_gb(ram_gb):
    """The RAM as it was sold: what psutil reports is less than the nominal size
    (memory reserved for graphics / firmware), e.g. 7.8 -> 8, 15.7 -> 16,
    13.9 -> 16, 11.8 -> 12. Within 15% below a nominal size it rounds up to it;
    otherwise to the nearest whole GB (10.0 -> 10). 0 when unknown."""
    try:
        ram = float(ram_gb)
    except (TypeError, ValueError):
        return 0
    if ram <= 0:
        return 0
    for nominal in _NOMINAL_GB:
        if nominal >= ram - 0.05:
            if nominal - ram <= 0.15 * nominal:
                return nominal
            break
    return max(1, int(ram + 0.5))         # half up (round() would make 6.5 -> 6)


def ram_gb():
    """Total RAM in GiB as the OS reports it, 0.0 when unknown."""
    try:
        import psutil
        return float(psutil.virtual_memory().total) / (1024 ** 3)
    except Exception:
        return 0.0


def physical_cores():
    """Physical cores (logical when the OS can't tell), 0 when unknown."""
    try:
        import psutil
        n = psutil.cpu_count(logical=False) or psutil.cpu_count(logical=True) or 0
        return int(n)
    except Exception:
        return 0


# ── CPU ───────────────────────────────────────────────────────────────────────
def clean_cpu_name(name):
    """Strip marketing noise: (R)/(TM), 'CPU'/'Processor', '@ 2.10GHz', '16-Core'.
    'Intel(R) Core(TM) i7-8650U CPU @ 1.90GHz' -> 'Intel Core i7-8650U'."""
    if not name:
        return None
    name = re.sub(r"\(R\)|\(TM\)|\(tm\)", "", str(name))
    name = re.sub(r"\bCPU\b|\bProcessor\b|\d+-Core", "", name, flags=re.I)
    name = re.sub(r"@.*$", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or None


def short_cpu_name(name):
    """The short form the 'Your computer' chip uses:
    '12th Gen Intel Core i7-1260P' -> 'i7-1260P', 'AMD Ryzen 7 5800H with Radeon
    Graphics' -> 'Ryzen 7 5800H', 'Apple M2 Pro' stays."""
    name = clean_cpu_name(name)
    if not name:
        return None
    name = re.sub(r"^\d+(st|nd|rd|th)\s+Gen(eration)?\s+", "", name, flags=re.I)
    name = re.sub(r"\s+with\s+.*$", "", name, flags=re.I)        # "... with Radeon Graphics"
    name = re.sub(r"\s+-\s+.*$", "", name)                        # "Snapdragon X Elite - X1E78100 - ..."
    name = re.sub(r"^(Intel|AMD)\s+", "", name, flags=re.I)
    name = re.sub(r"^Core\s+(?=i\d)", "", name, flags=re.I)       # "Core i7-1260P" -> "i7-1260P"
    return name.strip() or None


def _probe_cpu_name():
    """The raw processor name from the OS, or None."""
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return winreg.QueryValueEx(k, "ProcessorNameString")[0]
        if sys.platform == "darwin":
            return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                                  capture_output=True, text=True, timeout=4).stdout.strip()
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if "model name" in line:
                    return line.split(":", 1)[1].strip()
    except Exception:
        return None
    return None


def cpu_name():
    """Friendly processor name, e.g. 'Intel Core i7-14700' or 'AMD Ryzen 7'
    (ui/settings.py Settings._cpu_name). Cached for the run."""
    return _cached("cpu", lambda: clean_cpu_name(_probe_cpu_name()))


# ── GPU ───────────────────────────────────────────────────────────────────────
_DISCRETE = ("nvidia", "geforce", "rtx", "gtx", "quadro", "tesla", "radeon", "arc", "instinct")
# Adapters that aren't a graphics card worth naming.
_NOT_A_GPU = ("basic display", "basic render", "remote display", "virtual", "parsec",
              "displaylink", "mirror", "citrix", "vmware svga", "hyper-v")


def clean_gpu_name(name):
    name = re.sub(r"\(R\)|\(TM\)|\(tm\)", "", str(name or ""))
    return re.sub(r"\s+", " ", name).strip()


def _probe_gpu_names():
    """Every display adapter's raw name (Windows: CIM via PowerShell; Mac:
    system_profiler). [] when unknown."""
    names = []
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_VideoController).Name -join '|'"],
                capture_output=True, text=True, timeout=6,
                # The app is a windowed (no-console) build: without this flag
                # the child PowerShell pops a visible terminal window.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            names = [n.strip() for n in (out.stdout or "").split("|") if n.strip()]
        elif sys.platform == "darwin":
            out = subprocess.run(["system_profiler", "SPDisplaysDataType"],
                                 capture_output=True, text=True, timeout=6)
            for line in (out.stdout or "").splitlines():
                if "Chipset Model" in line:
                    names.append(line.split(":", 1)[1].strip())
    except Exception:
        names = []
    return names


def detect_gpus():
    """Cleaned names of the real display adapters (virtual ones dropped). Cached."""
    def _probe():
        out = []
        for n in _probe_gpu_names():
            c = clean_gpu_name(n)
            if c and not any(k in c.lower() for k in _NOT_A_GPU) and c not in out:
                out.append(c)
        return tuple(out)
    return list(_cached("gpus", _probe))


def pick_gpu(names, platform=None):
    """(name, discrete) of the GPU worth showing: the first discrete card, else
    the first adapter (integrated graphics). Mac: the chipset (Apple Silicon /
    Intel). ('', False) when there is none."""
    names = [clean_gpu_name(n) for n in (names or []) if clean_gpu_name(n)]
    if not names:
        return "", False
    if (platform or sys.platform) == "darwin":
        return names[0], False
    for n in names:
        if any(k in n.lower() for k in _DISCRETE):
            return n, True
    return names[0], False


def detect_gpu():
    """The primary discrete GPU's name, '' with only integrated graphics - the
    exact behaviour of ui/settings.py Settings._detect_gpu (Mac: the chipset)."""
    name, discrete = pick_gpu(detect_gpus())
    if sys.platform == "darwin":
        return name
    return name if discrete else ""


def gpu_state(cfg=None):
    """gpu_accel.status(cfg): 'unsupported' / 'ready' / 'available' / 'failed'.
    Imports ctranslate2 the first time (~0.3 s): worker thread only."""
    try:
        import gpu_accel
        return gpu_accel.status(cfg)
    except Exception as e:
        logger.debug("GPU status unavailable: %s", e)
        return "unsupported"


# ── The recommendation ────────────────────────────────────────────────────────
GPU_STATES_FOR_GPU = ("ready", "available")


@dataclass(frozen=True)
class Recommendation:
    model: str                    # a MODEL_INFO id
    suggest_cloud: bool = False   # also suggest Gusan Cloud (a weak computer)
    gpu: bool = False             # chosen because of an NVIDIA GPU
    needs_gpu_download: bool = False   # gpu_state 'available': offer the ~528 MB GPU download
    reason: str = ""              # gpu / cpu_8gb_6cores / cpu_4gb / low_memory / unknown

    @property
    def plain(self):
        return plain_name(self.model)


def recommend_model(ram_gb, cores, gpu_state=None, platform=None) -> Recommendation:
    """The speech model for this computer (see the module docstring).

    ram_gb     what psutil reports (GiB); compared after display_ram_gb rounding
    cores      physical cores
    gpu_state  gpu_accel.status(): ready / available / failed / unsupported / None
    platform   sys.platform by default; 'darwin' never uses the GPU
    Unknown RAM (None / 0) keeps the app default, 'base'."""
    platform = platform or sys.platform
    nominal = display_ram_gb(ram_gb)
    if nominal <= 0:
        return Recommendation(DEFAULT_SPEECH_MODEL, reason="unknown")
    try:
        cores = int(cores or 0)
    except (TypeError, ValueError):
        cores = 0
    gpu_ok = platform != "darwin" and gpu_state in GPU_STATES_FOR_GPU
    if gpu_ok and nominal >= 8:
        return Recommendation("large-v3-turbo", gpu=True,
                              needs_gpu_download=(gpu_state == "available"), reason="gpu")
    if nominal >= 8 and cores >= 6:
        return Recommendation("small", reason="cpu_8gb_6cores")
    if nominal >= 4:
        return Recommendation("base", reason="cpu_4gb")
    return Recommendation("tiny", suggest_cloud=True, reason="low_memory")


def runs_on_gpu(gpu_state, cuda_usable=None, platform=None):
    """Whether local Whisper really runs on the GPU: cuBLAS present ('ready') and
    not proven broken this run (recorder._cuda_usable False). Never on a Mac."""
    if (platform or sys.platform) == "darwin":
        return False
    return gpu_state == "ready" and cuda_usable is not False


def slower_here(model_id, on_gpu):
    """True when a big model will feel slow on this computer (no GPU): the
    mockup's 'A little slower here' under Most accurate."""
    return (not on_gpu) and model_id in ("medium", "large-v3-turbo", "large-v3")


def speed_phrase(speed_rank, on_gpu):
    """Plain-language speed for a model card (ui/settings.py _speed_phrase)."""
    try:
        rank = int(speed_rank)
    except (TypeError, ValueError):
        rank = 3
    if on_gpu:
        words = {1: "Instant", 2: "Instant", 3: "Very fast", 4: "Very fast", 5: "Fast", 6: "Fast"}
        return f"{words.get(rank, 'Fast')} on your GPU"
    words = {1: "Very fast", 2: "Fast", 3: "Fast", 4: "Moderate", 5: "Slow", 6: "Slower"}
    return f"{words.get(rank, 'Moderate')} on your CPU"


# ── The snapshot ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class HardwareSnapshot:
    cpu_name: Optional[str]       # 'Intel Core i7-1260P', None when unknown
    cores: int                    # physical cores, 0 when unknown
    ram_gb: float                 # as reported (GiB), 0.0 when unknown
    ram_display_gb: int           # rounded to the nominal size (16)
    gpu_name: str                 # discrete card first, else integrated; '' none
    gpu_discrete: bool
    gpu_state: Optional[str]      # gpu_accel.status(); None when the GPU wasn't probed
    platform: str
    recommended: Recommendation = field(default_factory=lambda: Recommendation(DEFAULT_SPEECH_MODEL))

    # The 'Your computer' chips (onboarding step 2, Settings › This computer).
    @property
    def cpu_chip(self):
        """'i7-1260P · 12 cores' ('12 cores' / '' when the name is unknown)."""
        short = short_cpu_name(self.cpu_name)
        cores = f"{self.cores} cores" if self.cores else ""
        return " · ".join(p for p in (short, cores) if p)

    @property
    def ram_chip(self):
        """'16 GB memory' ('' when unknown)."""
        return f"{self.ram_display_gb} GB memory" if self.ram_display_gb else ""

    @property
    def gpu_chip(self):
        """'Intel Iris Xe' ('' when there is no GPU worth naming)."""
        return re.sub(r"\s+Graphics$", "", self.gpu_name or "", flags=re.I).strip()

    @property
    def on_gpu(self):
        return runs_on_gpu(self.gpu_state, platform=self.platform)

    def to_dict(self):
        d = asdict(self)
        d["cpu_chip"], d["ram_chip"], d["gpu_chip"] = self.cpu_chip, self.ram_chip, self.gpu_chip
        return d


def snapshot(cfg=None, probe_gpu=True, platform=None) -> HardwareSnapshot:
    """Everything above in one value. Worker thread only (see the module docstring).
    probe_gpu=False skips the slow GPU probes (gpu_name '', gpu_state None) for a
    first instant answer; the recommendation then uses the CPU rules."""
    platform = platform or sys.platform
    ram = ram_gb()
    cores = physical_cores()
    cpu = cpu_name()
    if probe_gpu:
        gname, discrete = pick_gpu(detect_gpus(), platform)
        state = gpu_state(cfg)
    else:
        gname, discrete, state = "", False, None
    return HardwareSnapshot(
        cpu_name=cpu, cores=cores, ram_gb=round(ram, 2), ram_display_gb=display_ram_gb(ram),
        gpu_name=gname, gpu_discrete=discrete, gpu_state=state, platform=platform,
        recommended=recommend_model(ram, cores, state, platform),
    )


# ── Cache (the CPU and the adapters can't change while the app runs) ─────────
_cache = {}
_cache_lock = threading.Lock()


def _cached(key, probe):
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    value = probe()          # outside the lock: the GPU probe can take seconds
    if not value:
        # Nothing found: often a probe that failed or timed out (PowerShell
        # CIM on a busy first boot). Don't keep that for the whole run - the
        # next snapshot (e.g. probe_hardware(force=True)) asks again.
        return value
    with _cache_lock:
        return _cache.setdefault(key, value)


def clear_cache():
    """Forget the cached CPU / GPU names (tests; a later probe re-reads them)."""
    with _cache_lock:
        _cache.clear()
