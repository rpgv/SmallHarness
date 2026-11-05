"""A pool of model instances sized by available RAM.

How RAM is counted depends on the backend's `memory` kind:
  resident  each instance holds the model; instances are added while the next one fits.
  per_call  each call starts a process that loads the model. Instances are cheap slots,
            and the number of calls running at once is limited by RAM measured while
            the pool is idle (a reading taken mid-call would miss processes still loading).
  external  a server owns the model; concurrency is the server's max_concurrent.

With [resources].manage_ram = false (the default) none of this applies: the pool runs
exactly one instance, never refuses to load for lack of RAM, and leaves memory to the
OS. Either way, a model file over [resources].warn_ram_share of total RAM is warned about.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Callable, Iterator

from .backends.base import Backend
from .context.embed import ram_mb
from .resources import CapacityReport, RamInfo, capacity, model_file_mb, ram_share_warning


class ModelPool:
    def __init__(self, cfg, factory: Callable[[], Backend]):
        self.cfg = cfg
        self.factory = factory
        self.instances: list[Backend] = []
        self.busy: set[int] = set()
        self._cond = threading.Condition()  # reentrant (RLock)
        self._spare: Backend | None = None   # constructed, not yet loaded
        self._per_call_limit: int | None = None

    def _peek(self) -> Backend:
        if self.instances:
            return self.instances[0]
        if self._spare is None:
            self._spare = self.factory()
        return self._spare

    @property
    def memory(self) -> str:
        return self._peek().memory

    @property
    def ram_managed(self) -> bool:
        return bool(self.cfg["resources"].get("manage_ram", False))

    def report(self) -> CapacityReport:
        rep = self._measured_report() if self.ram_managed else self._unmanaged_report()
        rep.warning = ram_share_warning(rep.model_file_mb, self.cfg["resources"], rep.ram.total_mb or None)
        return rep

    def ram_warning(self) -> str:
        """The share-of-RAM warning alone, without measuring free RAM."""
        if self.cfg["model"]["backend"] == "mock" or self.memory == "external":
            return ""
        res = self.cfg["resources"]
        path = self.cfg["model"]["path"]
        mb = res.get("model_mb") or (model_file_mb(path) if path else 0)
        return ram_share_warning(mb, res)

    def _measured_report(self) -> CapacityReport:
        m = self.cfg["model"]
        kind = self.memory
        res = self.cfg["resources"]
        embed_mb = ram_mb(self.cfg["rag"]["embed"])  # an in-process embedding model needs RAM too
        if embed_mb:
            res = {**res, "reserve_ram_mb": res["reserve_ram_mb"] + embed_mb}
        if m["backend"] == "mock" or kind == "external":
            path, res = "", {**res, "model_mb": 0}
        else:
            path = m["path"]
        resident = len(self.instances) if kind == "resident" else 0
        rep = capacity(path, self.cfg.context_tokens, res, already_loaded=resident)
        if kind == "external":
            rep.max_instances = self._peek().max_concurrent or 1
            rep.note = "Model RAM is owned by an external server; not checked here"
        elif rep.footprint_mb == 0 and m["backend"] != "mock":
            rep.max_instances = 1  # unknown model size: no basis for running more than one
            rep.note = ("Model size unknown ([model].path is not a file): limited to 1 instance; "
                        "set [resources].model_mb to allow more")
        return rep

    def _unmanaged_report(self) -> CapacityReport:
        """Measured for information only; always fits, always one instance."""
        try:
            rep = self._measured_report()
        except Exception:  # RAM unreadable on this system: nothing to show
            rep = CapacityReport(RamInfo(0, 0, "unavailable"), 0, 0, 0, 1, True)
        rep.fits, rep.max_instances = True, 1
        rep.note = ("RAM is left to the OS ([resources].manage_ram = false): one instance, no RAM checks "
                    "before loading.")
        return rep

    @property
    def capacity(self) -> int:
        if not self.ram_managed:
            return 1
        kind = self.memory
        if kind != "per_call":
            return max(1, self.report().max_instances)
        with self._cond:
            if not self.busy or self._per_call_limit is None:
                rep = self.report()
                if not rep.fits:
                    raise RuntimeError("not enough free RAM to start the model now:\n" + rep.describe())
                self._per_call_limit = max(1, rep.max_instances)
            return self._per_call_limit

    def spawn(self, n: int = 1) -> int:
        """Start up to n more instances if RAM allows. Returns how many were started."""
        started = 0
        for _ in range(n):
            with self._cond:
                if self.instances and len(self.instances) >= self.capacity:
                    break
                inst, self._spare = self._spare or self.factory(), None
                inst.load()
                self.instances.append(inst)
                self._cond.notify_all()
            started += 1
        return started

    def ensure_one(self) -> None:
        if not self.instances:
            report = self.report()
            if not report.fits:
                raise RuntimeError("model does not fit in available RAM:\n" + report.describe())
            self.spawn(1)

    def _limit(self) -> int:
        return len(self.instances) if self.memory == "resident" else min(len(self.instances), self.capacity)

    @contextmanager
    def acquire(self) -> Iterator[Backend]:
        self.ensure_one()
        with self._cond:
            while True:
                free = [i for i in range(len(self.instances)) if i not in self.busy]
                if free and len(self.busy) < self._limit():
                    idx = free[0]
                    self.busy.add(idx)
                    break
                self._cond.wait()
        try:
            yield self.instances[idx]
        finally:
            with self._cond:
                self.busy.discard(idx)
                self._cond.notify_all()

    def parallel(self, fn: Callable, items: list) -> list:
        """fn(item) for every item, on as many instances as there are items and the
        RAM check allows (one when RAM is not managed). Results in item order."""
        if len(items) > 1:
            self.ensure_one()
            want = min(len(items), max(1, self.capacity))
            if len(self.instances) < want:
                self.spawn(want - len(self.instances))
        if len(self.instances) <= 1 or len(items) <= 1:
            return [fn(x) for x in items]
        return self.map(fn, items)

    def map(self, fn: Callable, items: list) -> list:
        """Run fn(item) concurrently; acquire() inside fn bounds how many run at once."""
        workers = max(1, len(self.instances))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(fn, items))

    def status(self) -> str:
        return f"{len(self.busy)}/{len(self.instances)} busy, capacity {self.capacity} ({self.memory})"

    def shrink(self) -> int:
        """Stop idle instances above the current capacity (after a settings change).
        Returns how many were stopped."""
        stopped = 0
        with self._cond:
            while len(self.instances) > max(1, self.capacity) and (len(self.instances) - 1) not in self.busy:
                self.instances.pop().close()
                stopped += 1
        return stopped

    def close(self) -> None:
        for inst in self.instances:
            inst.close()
        self.instances.clear()
