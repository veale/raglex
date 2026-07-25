"""In-app orchestration of the UCL-Myriad bulk-embed relay (hpc/README.md).

The manual relay is: ``embed-export`` → rsync → ``qsub`` array job → poll ``qstat`` →
rsync back → ``embed-import`` → ``index``. This module drives that whole loop as ONE
resumable operation — runnable as the ``hpc-embed`` background job or ``raglex hpc-embed``.

Why this shape, and its limits:

- **The two ends can't talk directly.** The DB is on asahi; the GPUs are on Myriad compute
  nodes with no inbound network. But the Myriad *login* node is reachable over SSH, so the
  orchestrator drives everything from the DB side via ``ssh <host>`` + ``rsync`` — using a
  host ALIAS from your ``~/.ssh/config`` (default ``myriad``), so ProxyJump/keys stay in
  SSH's hands and no credential ever touches the app. (Runs where SSH to Myriad works: an
  asahi shell, or with ``~/.ssh`` mounted into the container.)
- **Queueing is slow and lumpy.** ``qsub`` returns immediately but tasks may sit pending for
  a long time; the poll loop sleeps ``poll_seconds`` between ``qstat`` checks and reports
  state to the Jobs panel rather than blocking.
- **Nothing overruns.** The array job carries its own SGE wallclock (``h_rt``); the
  orchestrator carries its own ``deadline_hours`` — past it, it stops cleanly (the relay is
  resumable per shard, so re-running continues) and alerts, rather than running up GPU
  hours forever. Tasks that hit their wallclock with shards left are auto-resubmitted (the
  worker skips shards that already have a ``.vec.*``).
- **Postgres-friendly.** Chunking + the final ``embed-import`` (family-validated) + HNSW
  index build all happen on the DB side; only inference runs on the cluster.

**Safety**: dry-run by default — it prints the exact plan and every remote command without
running them. Pass ``go=True`` (job) / ``--go`` (CLI) to execute. It is never wired into the
automatic scheduler: a paid GPU submission is always an explicit, human act.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("raglex.hpc")

# phases, in order — the resume cursor is simply "the first phase not yet stamped done"
PHASES = ("export", "ship", "submit", "poll", "fetch", "import", "index")


@dataclass
class HpcSettings:
    host: str = "myriad"                       # ~/.ssh/config alias (ProxyJump/keys there)
    remote_dir: str = "~/Scratch/raglex-embed"
    venv: str = "~/Scratch/raglex-venv"
    model: str = "Qwen/Qwen3-Embedding-0.6B"
    revision: str = "1"
    dimensions: int = 1024
    ntasks: int = 40                           # array size; must match the jobscript -t range
    wallclock: str = "8:00:00"                 # SGE h_rt per task
    mem: str = "48G"
    gpu: str = "1"
    prefer_a100: bool = True                   # -ac allow=L
    poll_seconds: int = 120                    # qstat cadence (queueing is lumpy)
    deadline_hours: float = 24.0               # orchestrator hard stop (no runaway GPU spend)
    scope_sources: Optional[list[str]] = None  # jurisdiction scope (embed_source_scope)

    @classmethod
    def from_facade(cls, facade, **overrides) -> "HpcSettings":
        g = facade.settings.resolve
        def _int(k, d):
            try: return int(g(k) or d)
            except (TypeError, ValueError): return d
        s = cls(
            host=g("RAGLEX_HPC_HOST") or "myriad",
            remote_dir=g("RAGLEX_HPC_REMOTE_DIR") or "~/Scratch/raglex-embed",
            venv=g("RAGLEX_HPC_VENV") or "~/Scratch/raglex-venv",
            model=g("RAGLEX_HPC_MODEL") or g("RAGLEX_EMBED_MODEL") or "Qwen/Qwen3-Embedding-0.6B",
            revision=g("RAGLEX_HPC_REVISION") or "1",
            dimensions=_int("RAGLEX_HPC_DIMENSIONS", 1024),
            ntasks=_int("RAGLEX_HPC_NTASKS", 40),
            wallclock=g("RAGLEX_HPC_WALLCLOCK") or "8:00:00",
            mem=g("RAGLEX_HPC_MEM") or "48G",
            gpu=g("RAGLEX_HPC_GPU") or "1",
            poll_seconds=_int("RAGLEX_HPC_POLL_SECONDS", 120),
            deadline_hours=float(g("RAGLEX_HPC_DEADLINE_HOURS") or 24),
            scope_sources=facade.embed_source_scope(),
        )
        for k, v in overrides.items():
            if v is not None:
                setattr(s, k, v)
        return s


class Relay:
    """SSH/rsync command layer. Every method is a thin, logged shell-out; ``dry_run`` prints
    the command and returns success without touching the network."""

    def __init__(self, cfg: HpcSettings, *, dry_run: bool = True, emit: Callable = print) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.emit = emit

    def _run(self, argv: list[str], *, capture: bool = True, timeout: int = 1800):
        printable = " ".join(shlex.quote(a) for a in argv)
        if self.dry_run:
            self.emit(f"[dry-run] {printable}")
            return 0, "", ""
        self.emit(f"$ {printable}")
        proc = subprocess.run(argv, capture_output=capture, text=True, timeout=timeout)
        if proc.returncode != 0:
            log.warning("command failed (%s): %s", proc.returncode, proc.stderr.strip()[:400])
        return proc.returncode, (proc.stdout or ""), (proc.stderr or "")

    def ssh(self, remote_cmd: str, **kw):
        return self._run(["ssh", "-o", "BatchMode=yes", self.cfg.host, remote_cmd], **kw)

    def rsync(self, src: str, dst: str, *, extra: Optional[list[str]] = None, **kw):
        argv = ["rsync", "-avz", "--partial", *(extra or []), src, dst]
        return self._run(argv, **kw)

    # -- relay steps --------------------------------------------------------
    def ship(self, local_dir: Path, script_dir: Path) -> None:
        self.ssh(f"mkdir -p {shlex.quote(self.cfg.remote_dir)}")
        self.rsync(f"{local_dir}/", f"{self.cfg.host}:{self.cfg.remote_dir}/")
        for script in ("embed_shards.py", "myriad_embed.sh"):
            p = script_dir / script
            if p.exists():
                self.rsync(str(p), f"{self.cfg.host}:{self.cfg.remote_dir}/")

    def submit(self) -> Optional[str]:
        # -t 1-N must equal NTASKS in the jobscript; qsub echoes "Your job-array <id>.…"
        cmd = (f"cd {shlex.quote(self.cfg.remote_dir)} && "
               f"qsub -t 1-{self.cfg.ntasks} myriad_embed.sh")
        rc, out, _ = self.ssh(cmd)
        if self.dry_run:
            return "DRYRUN.1"
        for tok in out.split():
            if "." in tok and tok.split(".")[0].isdigit():
                return tok.split(".")[0]
        # fall back: "Your job-array 123456.1-40:1 ..." → 123456
        import re
        m = re.search(r"job-array\s+(\d+)", out)
        return m.group(1) if m else None

    def poll(self, job_id: str) -> dict:
        """qstat state for the array: how many tasks are still queued/running. When the job
        id no longer appears, the array is finished (done or dead)."""
        rc, out, _ = self.ssh(f"qstat -t 2>/dev/null | grep {shlex.quote(job_id)} || true")
        if self.dry_run:
            return {"present": False, "running": 0, "queued": 0}
        lines = [ln for ln in out.splitlines() if job_id in ln]
        running = sum(1 for ln in lines if " r " in f" {ln} ")
        queued = sum(1 for ln in lines if any(s in ln for s in (" qw ", " hqw ", " t ")))
        return {"present": bool(lines), "running": running, "queued": queued, "tasks": len(lines)}

    def remaining_shards(self) -> Optional[int]:
        """Shards still lacking a ``.vec.*`` on the remote — the true completion signal
        (a task can exit at wallclock with shards left; the count is what matters)."""
        cmd = (f"cd {shlex.quote(self.cfg.remote_dir)} 2>/dev/null && "
               "python3 - <<'PY'\n"
               "import glob,os\n"
               "sh=set(os.path.basename(f).split('.jsonl')[0] for f in glob.glob('shard-*.jsonl*'))\n"
               "ve=set(os.path.basename(f).split('.vec')[0] for f in glob.glob('*.vec.*'))\n"
               "print(len(sh-ve))\nPY")
        rc, out, _ = self.ssh(cmd)
        if self.dry_run:
            return None
        try:
            return int(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None

    def fetch(self, local_dir: Path) -> None:
        self.rsync(f"{self.cfg.host}:{self.cfg.remote_dir}/", f"{local_dir}/",
                   extra=["--include=*.vec.*", "--include=*/", "--exclude=*"])


class _State:
    """Resumable phase cursor persisted beside the shards, so a stop/restart (or a killed
    container) picks up at the first unfinished phase instead of re-shipping 20 GB."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {"phase": {}, "job_id": None}

    def done(self, phase: str) -> bool:
        return bool(self.data["phase"].get(phase))

    def mark(self, phase: str, value=True) -> None:
        self.data["phase"][phase] = value
        self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=1))


def run_hpc_embed(facade, params: dict, on_progress: Callable, cancel_check: Callable) -> dict:
    """Drive the whole Myriad embed relay as one resumable job. Dry-run unless ``go``."""
    from .embeddings.offline import export_shards, import_shards

    dry = not bool(params.get("go"))
    out_dir = Path(params.get("out") or (facade.config.data_dir / "embed-export"))
    out_dir.mkdir(parents=True, exist_ok=True)
    script_dir = Path(params.get("script_dir") or (Path(__file__).resolve().parent.parent.parent / "hpc"))
    cfg = HpcSettings.from_facade(
        facade, model=params.get("model"), dimensions=params.get("dimensions"),
        ntasks=params.get("ntasks"), deadline_hours=params.get("deadline_hours"))
    log_lines: list[str] = []
    def emit(msg: str) -> None:
        log_lines.append(msg)
        on_progress(stage="hpc", message=msg, phase=state.data["phase"], _checkpoint=state.data)
    state = _State(out_dir / "hpc-state.json")
    relay = Relay(cfg, dry_run=dry, emit=emit)
    started = time.time()
    def past_deadline() -> bool:
        return (time.time() - started) > cfg.deadline_hours * 3600

    emit(f"{'DRY-RUN — ' if dry else ''}Myriad embed relay: model={cfg.model} dims={cfg.dimensions} "
         f"ntasks={cfg.ntasks} host={cfg.host} scope={cfg.scope_sources or 'all'}")

    # 1. export (chunk the DB into shards) --------------------------------
    if not state.done("export"):
        emit("phase export: chunking the corpus into shards")
        if not dry:
            with facade._open() as (cat, _rs, ts):
                stats = export_shards(
                    cat, ts, out_dir, model=cfg.model, model_version=cfg.revision,
                    dimensions=cfg.dimensions, limit=params.get("pilot"),
                    sources=cfg.scope_sources,
                    on_progress=lambda **p: on_progress(stage="export", **p))
            state.mark("export", {"documents": stats.documents, "chunks": stats.chunks,
                                  "shards": stats.shards})
            emit(f"exported {stats.documents} docs / {stats.chunks} chunks / {stats.shards} shards")
        else:
            emit(f"[dry-run] would export shards to {out_dir} (pilot={params.get('pilot')})")
            state.mark("export", {"dry_run": True})

    for phase, action in (("ship", lambda: relay.ship(out_dir, script_dir)),):
        if not state.done(phase):
            emit(f"phase {phase}: rsync shards + scripts to {cfg.host}:{cfg.remote_dir}")
            action()
            state.mark(phase)

    # 3. submit -----------------------------------------------------------
    if not state.done("submit"):
        emit("phase submit: qsub array job")
        job_id = relay.submit()
        state.data["job_id"] = job_id
        state.mark("submit", {"job_id": job_id})
        emit(f"submitted array job {job_id}")

    # 4. poll (+ resubmit for wallclock-truncated shards) -----------------
    if not state.done("poll"):
        job_id = state.data.get("job_id")
        if dry:
            emit("[dry-run] would poll qstat until the array clears + all shards have vectors")
            state.mark("poll")
        else:
            while True:
                if cancel_check and cancel_check():
                    return {"cancelled": True, "phase": "poll", "log": log_lines}
                if past_deadline():
                    _alert(facade, "hpc_embed_deadline",
                           f"HPC embed exceeded deadline {cfg.deadline_hours}h; stopping (resumable)")
                    return {"error": "deadline exceeded", "phase": "poll",
                            "remaining_shards": relay.remaining_shards(), "log": log_lines}
                st = relay.poll(job_id) if job_id else {"present": False}
                remaining = relay.remaining_shards()
                on_progress(stage="poll", running=st.get("running"), queued=st.get("queued"),
                            remaining_shards=remaining, _checkpoint=state.data)
                if not st.get("present"):
                    if remaining and remaining > 0:
                        emit(f"array cleared but {remaining} shard(s) unfinished — resubmitting")
                        job_id = relay.submit()
                        state.data["job_id"] = job_id
                        state.save()
                    else:
                        emit("array complete; all shards embedded")
                        break
                time.sleep(cfg.poll_seconds)
            state.mark("poll")

    # 5. fetch ------------------------------------------------------------
    if not state.done("fetch"):
        emit(f"phase fetch: rsync vectors back to {out_dir}")
        relay.fetch(out_dir)
        state.mark("fetch")

    # 6. import (family-validated) ---------------------------------------
    if not state.done("import"):
        emit("phase import: writing vectors + FTS into Postgres")
        if not dry:
            with facade._open() as (cat, _rs, _ts):
                istats = import_shards(cat, out_dir,
                                       on_progress=lambda **p: on_progress(stage="import", **p))
            facade._invalidate_caches()
            state.mark("import", {"shards": istats.shards_imported, "docs": istats.documents,
                                  "chunks": istats.chunks})
            emit(f"imported {istats.shards_imported} shards / {istats.documents} docs / {istats.chunks} chunks")
        else:
            emit("[dry-run] would embed-import the fetched shards")
            state.mark("import", {"dry_run": True})

    # 7. index ------------------------------------------------------------
    if not state.done("index"):
        emit("phase index: (re)build the HNSW vector index")
        if not dry and hasattr(facade, "build_vector_index"):
            try:
                facade.build_vector_index()
            except Exception as exc:  # noqa: BLE001
                emit(f"index build skipped: {exc}")
        state.mark("index")

    emit("HPC embed relay complete" + (" (dry-run)" if dry else ""))
    return {"dry_run": dry, "phases": state.data["phase"], "job_id": state.data.get("job_id"),
            "out_dir": str(out_dir), "log": log_lines}


def _alert(facade, code: str, message: str) -> None:
    try:
        facade.push_alerts  # noqa: B018 — presence check
        log.warning("%s: %s", code, message)
    except Exception:  # noqa: BLE001
        pass
