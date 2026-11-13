#!/usr/bin/env python3
"""DataSentry command line.

One entry point for the whole system, so a fresh clone runs without a task
runner, a container runtime or a package install:

    py ds.py doctor          check the environment before anything else
    py ds.py demo            seed, inject, detect, correlate, investigate, act
    py ds.py serve           the API and the console
    py ds.py seed            build the world and its history
    py ds.py inject TYPE     inject a fault (or a decoy)
    py ds.py detect          score the recent cycles
    py ds.py correlate       group anomalies into incidents
    py ds.py rca             run the reasoning loop on open incidents
    py ds.py execute         run approved actions
    py ds.py verify          check whether remediations cleared
    py ds.py eval            score the harness against ground truth
    py ds.py recalibrate     turn human dispositions into detector tuning
    py ds.py status          where everything currently stands
    py ds.py faults          list the injectable fault types
    py ds.py reset           drop the control plane and start over
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for path in (ROOT, ROOT / "packages"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _prepare_console() -> None:
    """Windows consoles default to cp1252 and to ANSI-off. Fix both, quietly.

    Without this the box drawing in the output raises UnicodeEncodeError on a
    stock Windows Python, which is a poor first impression for a tool whose
    whole point is that it runs from a fresh clone.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(ValueError, OSError):
                stream.reconfigure(encoding="utf-8", errors="replace")
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            for handle in (-11, -12):  # stdout, stderr
                mode = ctypes.c_uint32()
                if kernel32.GetConsoleMode(kernel32.GetStdHandle(handle), ctypes.byref(mode)):
                    kernel32.SetConsoleMode(
                        kernel32.GetStdHandle(handle), mode.value | 0x0004
                    )
        except Exception:  # noqa: BLE001 - cosmetic only
            pass


_prepare_console()

from datasentry_core.logging import configure, paint  # noqa: E402
from datasentry_core.store import ControlPlane  # noqa: E402

BANNER = r"""
   ___       __       ____             __
  / _ \___ _/ /____ _/ __/__ ___  ___ / /_______ __
 / // / _ `/ __/ _ `/\ \/ -_) _ \/ _ / __/ __/ // /
/____/\_,_/\__/\_,_/___/\__/_//_/\_,_\__/_/  \_, /
                                            /___/
"""

DIM = "38;5;245"
ACCENT = "38;5;80"
GOOD = "38;5;114"
WARN = "38;5;214"
BAD = "38;5;203"


def banner() -> None:
    print(paint(BANNER, ACCENT))
    print(paint("  lakehouse reliability control plane", DIM))
    print(paint("  detection is statistics · reasoning is evidence-bound · action is deterministic", DIM))
    print()


def rule(title: str = "") -> None:
    width = 78
    if title:
        head = "── " + title + " "
        print(paint(head + "─" * max(0, width - len(head)), DIM))
    else:
        print(paint("─" * width, DIM))


def kv(label: str, value: object, *, colour: str = "") -> None:
    text = str(value)
    print("  " + paint(label.ljust(26), DIM) + (paint(text, colour) if colour else text))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_seed(args: argparse.Namespace) -> int:
    from tools.pipeline.pipeline import Pipeline

    store = ControlPlane()
    if args.fresh:
        store.reset()
    pipeline = Pipeline(store)
    rule("seeding the warehouse")
    report = pipeline.bootstrap(tables=args.tables, days=args.days, seed=args.seed)
    for key, value in report.detail.items():
        kv(key, value, colour=ACCENT if key in {"parse_coverage", "profiles"} else "")
    print()
    print(paint("  next: py ds.py inject JOIN_SEMANTICS", DIM))
    return 0


def cmd_inject(args: argparse.Namespace) -> int:
    from tools.pipeline.pipeline import Pipeline

    pipeline = Pipeline(ControlPlane())
    pipeline.resume()
    rule("injecting " + args.fault_type.upper())
    record, _report = pipeline.inject(args.fault_type.upper())
    kv("fault", record.fault_type, colour=WARN if not record.is_decoy else DIM)
    kv("root asset", record.root_asset_id)
    kv("column", record.root_column or "-")
    kv("mechanism", record.mechanism)
    kv("expected signals", ", ".join(str(s) for s in record.expected_signals))
    kv("expected downstream", len(record.expected_downstream))
    if record.pr_number:
        kv("landed as", "PR #" + str(record.pr_number) + " (" + str(record.commit_sha)[:7] + ")")
    if args.advance:
        advance = pipeline.advance(cycles=args.advance)
        kv("cycles advanced", advance.detail["cycles"])
    return 0


def cmd_detect(args: argparse.Namespace) -> int:
    from tools.pipeline.pipeline import Pipeline

    pipeline = Pipeline(ControlPlane())
    pipeline.resume()
    rule("detecting")
    report = pipeline.detect(cycles_back=args.cycles)
    for key, value in report.detail.items():
        kv(key, value, colour=WARN if key == "emitted" and value else "")
    return 0


def cmd_correlate(args: argparse.Namespace) -> int:
    from tools.pipeline.pipeline import Pipeline

    pipeline = Pipeline(ControlPlane())
    pipeline.resume()
    rule("correlating")
    report = pipeline.correlate()
    for key, value in report.detail.items():
        kv(key, value, colour=ACCENT if key == "compression_ratio" else "")
    return 0


def cmd_rca(args: argparse.Namespace) -> int:
    from tools.pipeline.pipeline import Pipeline

    pipeline = Pipeline(ControlPlane())
    pipeline.resume()
    rule("root cause analysis")

    def on_step(step) -> None:
        colour = GOOD if step.status == "ok" else BAD
        print(
            "  " + paint(("step " + str(step.seq)).ljust(8), DIM)
            + paint(step.node.ljust(16), colour)
            + paint(str(step.duration_ms) + "ms", DIM).ljust(20)
            + step.summary
        )

    report = pipeline.investigate(limit=args.limit,
                                  on_step=on_step if args.verbose else None,
                                  provider=getattr(args, "provider", None))
    print()
    for key, value in report.detail.items():
        kv(key, value)
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    from tools.pipeline.pipeline import Pipeline

    pipeline = Pipeline(ControlPlane())
    pipeline.resume()
    rule("executing approved actions")
    report = pipeline.execute_approved()
    for key, value in report.detail.items():
        kv(key, value)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from tools.pipeline.pipeline import Pipeline

    pipeline = Pipeline(ControlPlane())
    pipeline.resume()
    if args.advance:
        pipeline.advance(cycles=args.advance)
        pipeline.detect(cycles_back=args.advance)
    rule("verifying remediations")
    report = pipeline.verify()
    for key, value in report.detail.items():
        kv(key, value, colour=GOOD if key == "resolved" and value else
           (BAD if key == "escalated" and value else ""))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from tools.pipeline.demo import run_demo

    banner()
    return run_demo(
        tables=args.tables, days=args.days, seed=args.seed,
        faults=args.faults, quiet=args.quiet,
    )


def cmd_eval(args: argparse.Namespace) -> int:
    from evals.harness import run_evaluation

    banner()
    return run_evaluation(rebuild=args.rebuild, seeds=args.seeds, tables=args.tables,
                          provider=args.provider)


def cmd_recalibrate(args: argparse.Namespace) -> int:
    from services.detector.recalibration import RecalibrationJob, suggested_alpha

    store = ControlPlane()
    rule("recalibrating from human dispositions")
    report = RecalibrationJob(store).run()
    kv("dispositions read", report.dispositions)
    kv("anomalies relabelled", report.anomalies_relabelled)
    kv("cohorts scored", len(report.cohorts))

    if not report.cohorts:
        print()
        print(paint("  nothing to learn from yet — dispose of an incident first", DIM))
        return 0

    print()
    rule("observed against target")
    for entry in report.cohorts:
        colour = BAD if entry.drifted else GOOD
        print(
            "  " + paint(entry.cohort.ljust(16), DIM)
            + entry.signal.ljust(20)
            + ("observed " + format(entry.observed_fpr, ".3f")).ljust(18)
            + ("target " + format(entry.target_fpr, ".3f")).ljust(16)
            + paint("DRIFTED" if entry.drifted else "in band", colour)
        )
        if entry.drifted:
            print("      " + paint("suggested target -> "
                                   + format(suggested_alpha(entry), ".4f"), WARN))

    if report.health_incident:
        print()
        kv("health incident raised", report.health_incident[:8], colour=WARN)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = ControlPlane()
    rule("control plane")
    counts = {
        "assets": store.scalar("SELECT COUNT(*) FROM assets"),
        "columns": store.scalar("SELECT COUNT(*) FROM asset_columns"),
        "lineage edges": store.scalar("SELECT COUNT(*) FROM lineage_edges"),
        "profiles": store.scalar("SELECT COUNT(*) FROM profiles"),
        "anomalies": store.scalar("SELECT COUNT(*) FROM anomalies"),
        "incidents": store.scalar("SELECT COUNT(*) FROM incidents"),
        "evidence": store.scalar("SELECT COUNT(*) FROM evidence"),
        "hypotheses": store.scalar("SELECT COUNT(*) FROM hypotheses"),
        "actions": store.scalar("SELECT COUNT(*) FROM actions"),
        "faults injected": store.scalar("SELECT COUNT(*) FROM faults"),
        "audit rows": store.scalar("SELECT COUNT(*) FROM audit_log"),
    }
    for key, value in counts.items():
        kv(key, format(int(value), ","))

    incidents = store.list_incidents(limit=12)
    if incidents:
        print()
        rule("open incidents")
        for incident in incidents:
            colour = {1: BAD, 2: WARN, 3: DIM, 4: DIM}.get(incident.severity, DIM)
            print(
                "  " + paint("S" + str(incident.severity), colour)
                + "  " + paint(incident.incident_id[:8], DIM)
                + "  " + incident.title[:52].ljust(54)
                + paint(str(incident.status), DIM)
            )
    return 0


def cmd_faults(args: argparse.Namespace) -> int:
    from tools.faultgen import faults as faultlib

    rule("injectable faults")
    for name in faultlib.real_faults():
        print("  " + paint(name.ljust(22), WARN) + paint("fault", DIM))
    print()
    rule("decoys (must NOT raise an incident)")
    for name in faultlib.decoys():
        print("  " + paint(name.ljust(22), GOOD) + paint("decoy", DIM))
    return 0


def _row_counts(store: ControlPlane) -> dict[str, int]:
    """A missing table means an unseeded clone, not a crash."""
    out: dict[str, int] = {}
    for key, table in (("assets", "assets"), ("profiles", "profiles"),
                       ("incidents", "incidents")):
        try:
            out[key] = int(store.scalar("SELECT COUNT(*) FROM " + table) or 0)
        except Exception:  # noqa: BLE001
            out[key] = 0
    return out


def _port_is_free(host: str, port: int) -> bool:
    import socket

    probe = host if host not in {"0.0.0.0", "::"} else "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((probe, port)) != 0


def _snapshot_targets() -> list[tuple[str, Path]]:
    from datasentry_core.config import settings

    cfg = settings()
    return [("datasentry.db", Path(cfg.db_path)), ("warehouse.db", Path(cfg.warehouse_path))]


def cmd_snapshot(args: argparse.Namespace) -> int:
    """Write a compact, WAL-free copy of the seeded state to a directory.

    The Docker build runs the demo once and snapshots the result into the
    image, so a deployment on an ephemeral filesystem (Render free, Cloud Run,
    Hugging Face Spaces) starts with a populated warehouse in milliseconds
    instead of re-seeding for three minutes on every cold start.
    """
    import sqlite3

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    written = 0
    for name, source in _snapshot_targets():
        if not source.exists() or source.stat().st_size == 0:
            continue
        target = out / name
        target.unlink(missing_ok=True)
        with contextlib.closing(sqlite3.connect(source)) as conn:
            conn.execute("VACUUM INTO ?", (str(target),))
        kv(name, format(target.stat().st_size // 1024, ",") + " KiB", colour=GOOD)
        written += 1
    if not written:
        print("  " + paint("nothing to snapshot -- run `py ds.py demo` first", BAD))
        return 1
    return 0


def _restore_snapshot() -> bool:
    """Copy the baked-in snapshot into place when the data directory is empty.

    Only ever fills a missing database; it never overwrites live state, so a
    deployment with a persistent disk keeps its own history after first boot.
    """
    import os
    import shutil

    snap = os.environ.get("DS_SNAPSHOT_DIR", "").strip()
    if not snap or not Path(snap).is_dir():
        return False
    restored = False
    for name, target in _snapshot_targets():
        source = Path(snap) / name
        if not source.is_file() or (target.exists() and target.stat().st_size > 0):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        restored = True
    return restored


def cmd_doctor(args: argparse.Namespace) -> int:
    """Answer "why is this not working" before it is asked.

    Every check here corresponds to something that has actually gone wrong on
    a fresh machine: a Python too old for the type syntax, a missing
    dependency, an unseeded control plane, a port already taken by a previous
    `serve`, and -- the one that costs the most time -- a model id that the
    provider quietly retired. The last check is a live call, so `--offline`
    skips it when there is no network.
    """
    banner()
    rule("environment")

    ok = True
    version = ".".join(str(n) for n in sys.version_info[:3])
    supported = sys.version_info >= (3, 11)
    kv("python", version + ("" if supported else "  (needs 3.11+)"),
       colour=GOOD if supported else BAD)
    ok = ok and supported
    kv("platform", sys.platform)

    missing = []
    for module in ("fastapi", "uvicorn", "httpx", "pydantic"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        kv("dependencies", "missing: " + ", ".join(missing), colour=BAD)
        print("  " + paint("fix", DIM) + "  py -m pip install -e \".[dev]\"")
        ok = False
    else:
        kv("dependencies", "fastapi, uvicorn, httpx, pydantic", colour=GOOD)

    from datasentry_core.config import load_dotenv, settings

    cfg = settings()
    dotenv = load_dotenv()
    kv(".env", (str(len(dotenv)) + " keys read") if dotenv
       else "not present (optional; copy .env.example)", colour=GOOD if dotenv else DIM)
    kv("data dir", str(cfg.data_dir))

    print()
    rule("control plane")
    store = ControlPlane()
    counts = _row_counts(store)
    seeded = counts["assets"] > 0
    kv("database", str(cfg.db_path))
    kv("assets", format(counts["assets"], ","), colour=GOOD if seeded else WARN)
    kv("profiles", format(counts["profiles"], ","))
    kv("incidents", format(counts["incidents"], ","))
    if not seeded:
        print("  " + paint("fix", DIM) + "  py ds.py demo   " +
              paint("(seeds the world and runs the whole loop)", DIM))

    print()
    rule("reasoning")
    from datasentry_agent.providers import registry

    info = registry.describe(cfg)
    active = info["provider"]
    kv("configured", info["configured"])
    kv("active provider", active, colour=GOOD if active != "heuristic" else WARN)
    kv("cheap tier", info["cheap_model"])
    kv("frontier tier", info["frontier_model"])
    kv("keys present", ", ".join(info["keys_present"]) or "none")

    if active == "heuristic":
        print()
        print("  " + paint("The deterministic reasoner is answering. It works -- the demo and",
                           DIM))
        print("  " + paint("the eval suite run on it -- but narratives are blunter. For a free",
                           DIM))
        print("  " + paint("key: console.groq.com/keys  or  aistudio.google.com/apikey", DIM))
        print("  " + paint("then", DIM) + "  GROQ_API_KEY=... in .env")
    elif not args.offline:
        print()
        reasoner = registry.build_reasoner(cfg)
        health = reasoner.health()
        if not health.get("ok"):
            kv("live check", "failed: " + str(health.get("reason", "unknown")), colour=BAD)
            ok = False
        else:
            kv("live check", "key accepted", colour=GOOD)
            for tier in ("cheap", "frontier"):
                present = health.get(tier + "_available")
                name = health.get(tier, "?")
                if present:
                    kv(tier + " model", name + "  available", colour=GOOD)
                else:
                    kv(tier + " model", name + "  NOT in the catalogue", colour=BAD)
                    ok = False
            if not (health.get("cheap_available") and health.get("frontier_available")):
                sample = ", ".join(health.get("models", [])[:6])
                print("  " + paint("available", DIM) + "  " + sample)
                print("  " + paint("fix", DIM) + "  set DS_LLM_CHEAP_MODEL / "
                      "DS_LLM_FRONTIER_MODEL in .env")

    print()
    rule("serving")
    host = cfg.host
    port = cfg.port
    free = _port_is_free(host, port)
    kv("bind", host + ":" + str(port), colour=GOOD if free else WARN)
    if not free:
        kv("port", "already in use -- something is listening", colour=WARN)
        print("  " + paint("fix", DIM) + "  py ds.py serve --port 8788")
    kv("home", "http://localhost:" + str(port))
    kv("console", "http://localhost:" + str(port) + "/console")

    print()
    if ok:
        print("  " + paint("ready", GOOD) + "  " + paint("py ds.py serve", DIM))
    else:
        print("  " + paint("not ready", BAD) + "  " + paint("fix the lines above", DIM))
    print()
    return 0 if ok else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    banner()
    from datasentry_core.config import settings

    cfg = settings()
    host = args.host or cfg.host
    port = args.port or cfg.port
    shown = "localhost" if host in {"0.0.0.0", "127.0.0.1", "::"} else host
    url = "http://" + shown + ":" + str(port)

    if not _port_is_free(host, port):
        print("  " + paint("port " + str(port) + " is already in use", BAD))
        print("  " + paint("try", DIM) + "  py ds.py serve --port " + str(port + 1))
        print()
        return 1

    from datasentry_agent.providers import registry

    if _restore_snapshot():
        print("  " + paint("restored the seeded demo snapshot into", DIM) + " " + str(cfg.data_dir))
    info = registry.describe(cfg)
    store = ControlPlane()
    counts = _row_counts(store)

    print("  " + paint("home", DIM) + "      " + paint(url, ACCENT))
    print("  " + paint("console", DIM) + "   " + paint(url + "/console", ACCENT))
    print("  " + paint("api", DIM) + "       " + paint(url + "/api/v1", ACCENT))
    print("  " + paint("docs", DIM) + "      " + paint(url + "/api/docs", ACCENT))
    print("  " + paint("health", DIM) + "    " + paint(url + "/api/v1/health/detectors", ACCENT))
    print()
    print("  " + paint("reasoning", DIM) + " " +
          paint(info["provider"], GOOD if info["provider"] != "heuristic" else WARN) +
          paint("  (" + info["cheap_model"] + " / " + info["frontier_model"] + ")", DIM))
    if counts["assets"] == 0:
        print("  " + paint("warehouse", DIM) + " " + paint("empty", WARN) +
              paint("  -- run `py ds.py demo` in another terminal to fill it", DIM))
    else:
        print("  " + paint("warehouse", DIM) + " " +
              paint(format(counts["assets"], ",") + " assets, "
                    + format(counts["incidents"], ",") + " incidents", DIM))
    print()
    print("  " + paint("ctrl-c to stop", DIM))
    print()

    if getattr(args, "open", False):
        import threading
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run("services.api.app:app", host=host, port=port, log_level="warning",
                reload=args.reload)
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    store = ControlPlane()
    store.reset()
    print(paint("  control plane reset", DIM))
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ds", description="DataSentry — lakehouse reliability control plane"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("seed", help="build the synthetic warehouse and its history")
    p.add_argument("--tables", type=int, default=200)
    p.add_argument("--days", type=int, default=21)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--fresh", action="store_true", help="drop the control plane first")
    p.set_defaults(fn=cmd_seed)

    p = sub.add_parser("inject", help="inject a fault or a decoy")
    p.add_argument("fault_type")
    p.add_argument("--advance", type=int, default=0, help="cycles to run afterwards")
    p.set_defaults(fn=cmd_inject)

    p = sub.add_parser("detect", help="score recent cycles")
    p.add_argument("--cycles", type=int, default=3)
    p.set_defaults(fn=cmd_detect)

    p = sub.add_parser("correlate", help="group anomalies into incidents")
    p.set_defaults(fn=cmd_correlate)

    p = sub.add_parser("rca", help="run the reasoning loop")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--provider", default=None,
                   choices=["groq", "gemini", "heuristic"],
                   help="override the configured reasoner for this run")
    p.set_defaults(fn=cmd_rca)

    p = sub.add_parser("execute", help="run approved actions")
    p.set_defaults(fn=cmd_execute)

    p = sub.add_parser("verify", help="check whether remediations cleared")
    p.add_argument("--advance", type=int, default=3)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("demo", help="the whole loop, end to end")
    # 60 tables over 12 days is the configuration every number in
    # BENCHMARKS.md was measured on, and it is the floor the fault harness
    # needs to place independent faults. The demo defaulting to 200 tables
    # meant the headline command was six times the work of the benchmark it
    # quoted its timing from -- a first run that took a quarter of an hour
    # while the README promised two minutes. `--tables 200` is still there
    # for the full world.
    p.add_argument("--tables", type=int, default=60)
    p.add_argument("--days", type=int, default=12)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--faults", nargs="*", default=None)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("snapshot", help="write a compact copy of the seeded state")
    p.add_argument("out", help="directory to write datasentry.db (and warehouse.db) into")
    p.set_defaults(fn=cmd_snapshot)

    p = sub.add_parser("eval", help="score against ground truth")
    p.add_argument("--provider", default=None,
                   choices=["groq", "gemini", "heuristic"],
                   help="score the gold set against a specific reasoner")
    p.add_argument("--rebuild", action="store_true")
    p.add_argument("--seeds", type=int, default=3)
    p.add_argument("--tables", type=int, default=80)
    p.set_defaults(fn=cmd_eval)

    p = sub.add_parser("recalibrate", help="turn human dispositions into detector tuning")
    p.set_defaults(fn=cmd_recalibrate)

    p = sub.add_parser("status", help="where everything stands")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("faults", help="list injectable faults")
    p.set_defaults(fn=cmd_faults)

    p = sub.add_parser("doctor", help="check the environment before anything else")
    p.add_argument("--offline", action="store_true",
                   help="skip the live provider check")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("serve", help="run the API and console")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--reload", action="store_true")
    p.add_argument("--open", action="store_true",
                   help="open the console in a browser once it is up")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("reset", help="drop the control plane")
    p.set_defaults(fn=cmd_reset)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure("ds")
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        print()
        return 130
    except (RuntimeError, ValueError) as exc:
        # A misconfigured run deserves a message, not a traceback. Anything
        # else still raises, because an unexpected failure should look like one.
        print(paint("  " + str(exc), BAD))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
