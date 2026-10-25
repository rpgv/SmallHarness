"""RAM detection and model-footprint estimation, used to decide how many
model instances can run side by side."""
from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .modelinfo import read_model_info


@dataclass
class RamInfo:
    total_mb: int
    available_mb: int
    source: str


def ram_info() -> RamInfo:
    try:
        import psutil  # optional
        vm = psutil.virtual_memory()
        return RamInfo(vm.total // 2**20, vm.available // 2**20, "psutil")
    except ImportError:
        pass
    system = platform.system()
    if system == "Linux":
        vals: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, rest = line.split(":", 1)
                vals[key] = int(rest.split()[0])  # kB
        avail = vals.get("MemAvailable", vals.get("MemFree", 0))
        info = RamInfo(vals["MemTotal"] // 1024, avail // 1024, "/proc/meminfo")
        return _apply_cgroup_limit(info)
    if system == "Darwin":
        total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"])) // 2**20
        out = subprocess.check_output(["vm_stat"], text=True)
        page = 4096
        free_pages = 0
        for line in out.splitlines():
            if "page size of" in line:
                page = int(line.split("page size of")[1].split()[0])
            elif line.startswith(("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable")):
                free_pages += int(line.split(":")[1].strip().rstrip("."))
        return RamInfo(total, free_pages * page // 2**20, "vm_stat")
    if system == "Windows":
        import ctypes

        class MEMSTAT(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMSTAT()
        st.dwLength = ctypes.sizeof(MEMSTAT)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return RamInfo(st.ullTotalPhys // 2**20, st.ullAvailPhys // 2**20, "GlobalMemoryStatusEx")
    raise RuntimeError(f"cannot read RAM on {system}; install psutil")


def _apply_cgroup_limit(info: RamInfo) -> RamInfo:
    """Containers report host RAM in /proc/meminfo; respect a cgroup v2 limit."""
    try:
        limit = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        used = int(Path("/sys/fs/cgroup/memory.current").read_text().strip())
    except (OSError, ValueError):
        return info
    if limit == "max":
        return info
    limit_mb = int(limit) // 2**20
    if limit_mb >= info.total_mb:
        return info
    return RamInfo(limit_mb, min(info.available_mb, limit_mb - used // 2**20), info.source + "+cgroup")


def model_file_mb(path: str | os.PathLike) -> int:
    p = Path(path)
    if p.is_dir():
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) // 2**20
    return p.stat().st_size // 2**20 if p.exists() else 0


def estimate_kv_cache_mb(file_mb: int, context_tokens: int) -> int:
    """Heuristic: ~50 KB of fp16 KV cache per token for every GB of weights
    (Gemma-3 1B ≈ 26 KB/token, 4B ≈ 140 KB/token)."""
    per_token_kb = max(8.0, 50.0 * file_mb / 1024)
    return int(per_token_kb * context_tokens / 1024)


@dataclass
class CapacityReport:
    ram: RamInfo
    model_file_mb: int
    footprint_mb: int          # estimated RAM per instance
    usable_mb: int             # available RAM minus reserve
    max_instances: int         # how many instances fit (capped by config)
    fits: bool
    note: str = ""

    def describe(self) -> str:
        lines = [
            f"RAM total {self.ram.total_mb} MB, available {self.ram.available_mb} MB ({self.ram.source})",
            f"Model file {self.model_file_mb} MB -> estimated {self.footprint_mb} MB per instance",
            f"Usable after reserve: {self.usable_mb} MB",
        ]
        if self.note:
            lines.append(self.note)
        if self.fits:
            lines.append(f"Capacity: {self.max_instances} instance(s)")
        else:
            lines.append("WARNING: the model does not fit in available RAM; expect swapping or OOM")
        return "\n".join(lines)


def kv_cache_mb(model_path: str, file_mb: int, context_tokens: int, res_cfg: dict) -> int:
    """The configured size, else exact from the model's KV-cache tensors, else the heuristic."""
    if not file_mb:
        return 0
    if res_cfg.get("kv_cache_mb"):
        return res_cfg["kv_cache_mb"]
    info = read_model_info(model_path) if model_path else None
    exact = info.kv_mb(context_tokens) if info else None
    return exact if exact is not None else estimate_kv_cache_mb(file_mb, context_tokens)


@dataclass
class ContextChoice:
    tokens: int
    note: str = ""            # how it was chosen, or a warning
    model_max: int | None = None


FALLBACK_CONTEXT = 4096


def choose_context(model_cfg: dict, res_cfg: dict, model_path: str, model_max: int | None = None,
                   source: str = "") -> ContextChoice:
    """The context window to use. A number in [model].context_tokens is used as is,
    lowered to the model's maximum if above it. "auto" (or 0) takes the model's
    maximum, capped at [model].auto_context_max and, when RAM is managed, lowered
    until one instance fits. `model_max`: the maximum when there is no model file to read
    it from (`source` says where it came from)."""
    setting = model_cfg.get("context_tokens", "auto")
    if model_cfg.get("backend") == "mock":
        return ContextChoice(setting if isinstance(setting, int) and setting > 0 else FALLBACK_CONTEXT)
    info = read_model_info(model_path) if model_path and model_cfg.get("backend") != "mock" else None
    model_max = info.max_context if info else model_max
    if isinstance(setting, int) and setting > 0:
        if model_max and setting > model_max:
            return ContextChoice(model_max, f"context_tokens {setting} is above the model's maximum "
                                            f"({model_max}); using {model_max}", model_max)
        return ContextChoice(setting, "", model_max)
    if not model_max:
        where = f"{source} does not report it" if source else "no readable .litertlm file"
        return ContextChoice(FALLBACK_CONTEXT, f"context size unknown ({where}); "
                                               f"using {FALLBACK_CONTEXT}: set [model].context_tokens", None)
    cap = int(model_cfg.get("auto_context_max") or model_max)
    tokens = min(model_max, cap)
    why = f"auto: the model supports {model_max} tokens" + (f" (per {source})" if source else "")
    why += f"; capped at auto_context_max {cap}" if tokens < model_max else ""
    if res_cfg.get("manage_ram", True):
        try:
            usable = ram_info().available_mb - res_cfg.get("reserve_ram_mb", 1024)
        except Exception:
            usable = None
        fmb = res_cfg.get("model_mb") or (model_file_mb(model_path) if model_path else 0)
        weights = int(fmb * res_cfg.get("overhead_factor", 1.25))
        if usable is not None and weights < usable:
            fitted = tokens
            while fitted > 2048 and weights + kv_cache_mb(model_path, fmb, fitted, res_cfg) > usable:
                fitted //= 2
            if fitted < tokens:
                why += f"; lowered to {fitted} to fit free RAM"
                tokens = fitted
    return ContextChoice(tokens, why, model_max)


def capacity(model_path: str, context_tokens: int, res_cfg: dict, already_loaded: int = 0) -> CapacityReport:
    """`already_loaded` instances are counted as using RAM that is no longer available."""
    ram = ram_info()
    fmb = res_cfg.get("model_mb") or (model_file_mb(model_path) if model_path else 0)
    kv = kv_cache_mb(model_path, fmb, context_tokens, res_cfg)
    footprint = int(fmb * res_cfg.get("overhead_factor", 1.25)) + kv
    usable = ram.available_mb - res_cfg.get("reserve_ram_mb", 1024)
    if footprint <= 0:  # no model file (e.g. mock backend)
        n = res_cfg.get("max_instances", 4)
        return CapacityReport(ram, fmb, 0, usable, n, True)
    extra = max(0, usable // footprint)
    n = min(res_cfg.get("max_instances", 4), already_loaded + extra)
    return CapacityReport(ram, fmb, footprint, usable, max(n, 0), already_loaded + extra >= 1)
