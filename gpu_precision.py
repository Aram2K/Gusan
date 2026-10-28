"""Which precision local Whisper runs at on an NVIDIA card (Gusan 2.0.1).

faster-whisper / CTranslate2 stays the engine on NVIDIA CUDA and on the CPU. On the CPU the
precision stays int8 (float32 is 1.3x slower, needs twice the memory and writes the same text;
int8 costs at most one word in 167). On NVIDIA the best precision is float16 - the weights as the
model was published, nothing compressed - and it is used whenever the card can run it and has the
memory; int8_float16 (weights in int8, maths in float16) is the step below, int8 the one under
that for old cards. A load that runs out of memory steps down one level and remembers it for that
model (main.AudioRecorder._build_model_locked). No Qt, no GPU libraries imported here: the
CTranslate2 module is passed in, so it is all testable with a fake.

CTranslate2's get_supported_compute_types("cuda") says float16 and int8_float16 for a card with
compute capability 7.0 or newer, and only int8 for the 6.x cards (they would run float16 as
float32), so asking it is the whole "does this card support it" check.
"""
import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger("transcribe")

# Best first: each is lighter on video memory than the one before.
LADDER = ("float16", "int8_float16", "int8")
LEGACY = "int8_float16"       # what 2.0.0 always used on the GPU: when the card can't be asked

# What a Whisper model needs on the card, from the size of its (float16) weights: the weights
# themselves (int8 weights are about 0.6 of that), the CUDA context and libraries, and the maths
# memory beam search uses (about 0.4 of the weights). Fitted to what faster-whisper publishes for
# large-v2 on a GPU, beam size 5: 4525 MB in float16 and 2926 MB in int8_float16. A guess that is a
# little high only means float16 is skipped on a card with just enough memory; one that is a little
# low is caught by the out-of-memory step-down - so it is safe either way.
BASE_MB = 300
WORK_SHARE = 0.4
INT8_SHARE = 0.6
SMI_TIMEOUT = 4.0


def supported_cuda_types(ct2=None):
    """The compute types CTranslate2 can run on the first CUDA card, as a tuple, or None when it
    can't say (an old build, or no card)."""
    try:
        if ct2 is None:
            import ctranslate2 as ct2
        return tuple(str(t) for t in ct2.get_supported_compute_types("cuda"))
    except Exception:
        return None


def best_cuda_type(ct2=None):
    """The best precision for the card: float16 when it is supported, else int8_float16, else int8
    (old cards); None when the card runs none of them (then the processor is better). A build that
    can't be asked gets what 2.0.0 always used (int8_float16)."""
    supported = supported_cuda_types(ct2)
    if supported is None:
        return LEGACY
    for ct in LADDER:
        if ct in supported:
            return ct
    return None


def lower_types(ct, supported=None, skip=()):
    """The precisions below ``ct`` that may still be tried: in the supported list (all of them
    when that isn't known) and not in ``skip`` (the ones that already ran out of memory)."""
    if ct not in LADDER:
        return []
    below = LADDER[LADDER.index(ct) + 1:]
    return [t for t in below if (supported is None or t in supported) and t not in set(skip)]


def need_mb(name, ct):
    """Video memory (MB) a Whisper model needs at this precision, or None for a model not known."""
    try:
        import hardware
        info = hardware.MODEL_INFO.get(name)
    except Exception:
        info = None
    if info is None or not getattr(info, "size_mb", 0):
        return None
    weights = float(info.size_mb)
    share = 1.0 if ct == "float16" else INT8_SHARE
    return int(weights * share + BASE_MB + weights * WORK_SHARE)


def plan(name, best, supported=None, free_mb=None, skip=()):
    """The ordered CUDA precisions to try for model ``name``: from ``best`` downwards, only those
    the card supports (when known), not those that ran out of memory before (``skip``), and not
    float16 when less video memory is free than the model needs in it (``free_mb`` None = not
    known: tried, and the out-of-memory step-down is the safety net). Returns (types, reason)
    where reason says why float16 was left out ("" when it wasn't)."""
    if best not in LADDER:
        return ([best] if best else []), ""
    chain = [best] + lower_types(best, supported)
    chain = [t for t in chain if t not in set(skip)]
    reason = ""
    if chain and chain[0] == "float16" and free_mb is not None:
        need = need_mb(name, "float16")
        if need is not None and free_mb < need:
            chain = chain[1:]
            reason = "only %d MB of video memory is free, float16 needs about %d MB" % (free_mb, need)
    return chain, reason


def _smi_paths():
    found = shutil.which("nvidia-smi")
    if found:
        yield found
    if sys.platform == "win32":
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        yield os.path.join(root, "System32", "nvidia-smi.exe")
        pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
        yield os.path.join(pf, "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe")


def free_vram_mb(run=None, paths=None):
    """Free video memory (MB) of the first NVIDIA card, from nvidia-smi (it comes with the driver);
    None when it can't be read. ~0.1-0.3 s: asked once per model load, never per piece.
    ``run`` / ``paths``: a fake subprocess.run and the nvidia-smi paths to try (tests)."""
    run = run or subprocess.run
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    if paths is None:
        paths = [p for p in _smi_paths() if os.path.isfile(p)]
    for exe in paths:
        try:
            res = run([exe, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                      capture_output=True, text=True, timeout=SMI_TIMEOUT, creationflags=flags)
        except Exception:
            continue
        if getattr(res, "returncode", 1) != 0:
            continue
        for line in str(getattr(res, "stdout", "") or "").splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
            break
    return None
