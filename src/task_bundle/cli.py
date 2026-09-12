"""Typer CLI entrypoint. Thin layer: parse args, delegate to library code, render output.

Every command runs inside ``record_command``, which writes a ``commands`` row to
SQLite at start and finish (even on crashes), creates the command's artifact
directory, and prints the command id so collaborators can query it later with
``task logs <command-id>``.
"""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from task_bundle import __version__
from task_bundle.bundle import Bundle, utc_now_iso
from task_bundle.container import Docker
from task_bundle.db import Database, new_id
from task_bundle.errors import BundleError, ContractViolation, DockerError, TaskError
from task_bundle.grading import (
    FLAKY,
    TestExecution,
    check_baseline_contract,
    check_gold_contract,
    consolidate,
)
from task_bundle.harness import IMAGE_REPO, ensure_image, run_baseline_suites, smoke_test
from task_bundle.report import build_report, tool_versions, write_report
from task_bundle.run import execute_run
from task_bundle.solver import ClaudeSolver, Solver, StubSolver
from task_bundle.swebench import convert_instance, fetch_instance
from task_bundle.workspace import clone_at_commit, resolve_repo_url

app = typer.Typer(
    name="task",
    help="Package, validate, and run LLM solvers against SWE-bench-style task bundles.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
runs_app = typer.Typer(help="Query past solver runs.", no_args_is_help=True)
app.add_typer(runs_app, name="runs")
console = Console()
err_console = Console(stderr=True, style="bold red")


@dataclass
class Settings:
    """Global persistence locations, overridable via --db/--artifacts-dir or env."""

    db_path: Path = Path.home() / ".task-bundle" / "task.db"
    artifacts_dir: Path = Path.home() / ".task-bundle" / "artifacts"


settings = Settings()


class CommandRecord:
    """Handle for the currently recorded command: logging, artifacts, test results."""

    def __init__(self, db: Database, command_id: str, artifact_dir: Path) -> None:
        self.db = db
        self.command_id = command_id
        self.artifact_dir = artifact_dir
        self._log_path = artifact_dir / "command.log"

    def log(self, message: str) -> None:
        """Append a line to the command's on-disk log."""
        with self._log_path.open("a") as f:
            f.write(f"{utc_now_iso()} {message}\n")

    def save_artifact(self, type_: str, filename: str, content: str) -> Path:
        """Write an artifact file and register its path in the DB."""
        path = self.artifact_dir / filename
        path.write_text(content)
        self.db.add_artifact(self.command_id, type_, str(path))
        return path

    def add_test_results(self, phase: str, executions: list[TestExecution]) -> None:
        self.db.record_test_results(self.command_id, phase, executions)


@contextmanager
def record_command(name: str, bundle_path: Path | None = None) -> Iterator[CommandRecord]:
    """Record a CLI invocation in SQLite, including its exit code on any outcome."""
    db = Database(settings.db_path)
    command_id = new_id("cmd")
    artifact_dir = settings.artifacts_dir / command_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    record = CommandRecord(db, command_id, artifact_dir)
    db.insert_command(
        command_id,
        name,
        json.dumps(sys.argv[1:]),
        str(bundle_path) if bundle_path else None,
        utc_now_iso(),
        str(record._log_path),
    )
    exit_code = 0
    try:
        yield record
    except TaskError as e:
        exit_code = e.exit_code
        record.log(f"error: {e}")
        raise
    except BaseException:
        exit_code = 1
        raise
    finally:
        db.finish_command(command_id, exit_code, utc_now_iso())
        db.close()
        console.print(f"[dim]command id: {command_id}[/dim]")


@app.callback()
def _root(
    version: Annotated[bool, typer.Option("--version", help="Show version and exit.")] = False,
    db: Annotated[
        Path | None,
        typer.Option(envvar="TASK_BUNDLE_DB", help="SQLite database path."),
    ] = None,
    artifacts_dir: Annotated[
        Path | None,
        typer.Option(envvar="TASK_BUNDLE_ARTIFACTS", help="Artifacts root directory."),
    ] = None,
) -> None:
    if db:
        settings.db_path = db
    if artifacts_dir:
        settings.artifacts_dir = artifacts_dir
    if version:
        console.print(f"task-bundle {__version__}")
        raise typer.Exit()


@app.command()
def init(
    bundle_path: Annotated[Path, typer.Argument(help="Bundle directory (created if scaffolding).")],
    repo: Annotated[
        str | None, typer.Option(help="Repository URL (required when scaffolding).")
    ] = None,
    commit: Annotated[str | None, typer.Option(help="Full 40-char commit SHA to pin.")] = None,
    base_image: Annotated[
        str, typer.Option(help="Base Docker image for the task.")
    ] = "python:3.11-slim",
    test_command: Annotated[
        str, typer.Option(help="Test command template; {test_path} is substituted per test.")
    ] = "python -m pytest {test_path} -x -q",
    setup: Annotated[
        list[str] | None,
        typer.Option("--setup", help="Setup command (repeatable), run at image build."),
    ] = None,
    force: Annotated[
        bool, typer.Option(help="Re-clone the workspace even if already present.")
    ] = False,
    build: Annotated[
        bool,
        typer.Option(
            "--build/--skip-build",
            help="Build and smoke-test the task image (requires Docker).",
        ),
    ] = True,
    rebuild: Annotated[bool, typer.Option(help="Force an image rebuild even if cached.")] = False,
) -> None:
    """Scaffold the bundle (if needed) and materialize the repo at the pinned commit.

    Two modes: with --repo/--commit on a fresh directory, scaffolds task.json,
    description.md, and the tests/ skeleton first; on an existing bundle, just
    (re-)initializes the workspace from task.json.
    """
    with record_command("init", bundle_path) as rec:
        if (bundle_path / "task.json").is_file():
            bundle = Bundle.load(bundle_path)
            if repo or commit:
                raise TaskError(
                    f"{bundle_path} already has a task.json; --repo/--commit are only for "
                    "scaffolding. Edit task.json directly to change the pin."
                )
        else:
            if not repo or not commit:
                raise TaskError(
                    "Scaffolding a new bundle requires --repo and --commit, e.g.\n"
                    f"  task init {bundle_path} --repo https://github.com/org/repo "
                    "--commit <full-sha>"
                )
            bundle = Bundle.scaffold(
                bundle_path,
                repo_url=repo,
                commit=commit,
                base_image=base_image,
                test_command=test_command,
                setup_commands=setup,
            )
            rec.log(f"scaffolded bundle at {bundle.path}")
            console.print(f"[green]Scaffolded[/green] bundle at [bold]{bundle.path}[/bold]")

        console.print(
            f"Cloning [bold]{bundle.spec.repo.url}[/bold] @ {bundle.spec.repo.commit[:12]} ..."
        )
        clone_at_commit(
            resolve_repo_url(bundle.spec.repo.url, bundle.path),
            bundle.spec.repo.commit,
            bundle.workspace_dir,
            force=force,
        )
        rec.log(f"workspace pinned to {bundle.spec.repo.commit}")
        state = bundle.load_state()
        if build:
            docker = Docker()
            docker.ensure_available()
            with console.status("Building task image (cached by content hash)..."):
                image, build_log = ensure_image(docker, bundle, rebuild=rebuild)
                smoke_test(docker, image)
            state.image_tag = image.tag
            state.image_digest = docker.image_id(image.tag)
            if build_log:
                rec.save_artifact("build_log", "image_build.log", build_log)
            rec.log(
                f"image ready: {image.tag} (digest {state.image_digest}, repo at "
                f"{image.repo_dir}), smoke test passed"
            )
            console.print(
                f"[green]Image ready[/green]: {image.tag} (repo at {image.repo_dir}, "
                "smoke test passed)"
            )
        state.status = "initialized"
        state.initialized_at = utc_now_iso()
        bundle.save_state(state)
        try:
            bundle.test_format()
            next_step = "run [bold]task validate[/bold]"
        except BundleError:
            next_step = (
                "add hidden tests under tests/fail2pass/ and tests/pass2pass/, "
                "then run [bold]task validate[/bold]"
            )
        console.print(
            f"[green]Initialized[/green] task [bold]{bundle.spec.id}[/bold] "
            f"(workspace pinned to {bundle.spec.repo.commit[:12]}).\nNext: {next_step}."
        )


@app.command()
def validate(
    bundle_path: Annotated[Path, typer.Argument(help="Bundle directory to validate.")],
    attempts: Annotated[int, typer.Option(help="Times to run each suite for flake detection.")] = 3,
    rebuild: Annotated[bool, typer.Option(help="Force an image rebuild even if cached.")] = False,
) -> None:
    """Check the baseline contract: pass2pass all pass, fail2pass all fail.

    Each suite runs --attempts times (default 3, per SWE-bench Pro) in fresh
    containers; tests with inconsistent statuses are flagged flaky. Exits 2 with
    specific reasons if the contract is violated.
    """
    with record_command("validate", bundle_path) as rec:
        bundle = Bundle.load(bundle_path)
        bundle.test_format()  # fail fast with a clear message if hidden tests are missing
        docker = Docker()
        docker.ensure_available()
        with console.status("Ensuring task image..."):
            image, build_log = ensure_image(docker, bundle, rebuild=rebuild)
        if build_log:
            rec.save_artifact("build_log", "image_build.log", build_log)
        console.print(f"Image: {image.tag}")
        with console.status(f"Running hidden test suites x{attempts} in fresh containers..."):
            executions = run_baseline_suites(docker, bundle, image, attempts=attempts)
        rec.add_test_results("baseline", executions)
        rec.save_artifact(
            "test_output",
            "baseline_tests.txt",
            "\n".join(
                f"=== {e.test} [{e.bucket}] attempt {e.attempt}: {e.status} ===\n{e.output}"
                for e in executions
            ),
        )
        results = consolidate(executions)

        table = Table(title=f"Baseline validation: {bundle.spec.id}")
        table.add_column("Test")
        table.add_column("Bucket")
        table.add_column("Attempts")
        table.add_column("Expected")
        table.add_column("Result")
        for r in results:
            expected = "fail" if r.bucket == "fail2pass" else "pass"
            ok = (r.status == "failed") if r.bucket == "fail2pass" else (r.status == "passed")
            style = "yellow" if r.status == FLAKY else ("green" if ok else "red")
            table.add_row(
                r.test,
                r.bucket,
                " ".join(r.attempt_statuses),
                expected,
                f"[{style}]{'OK' if ok else r.status.upper()}[/{style}]",
            )
        console.print(table)

        problems = check_baseline_contract(results)
        if problems:
            for p in problems:
                rec.log(f"contract violation: {p}")
                console.print(f"[bold red]contract violation:[/bold red] {p}")
            raise ContractViolation(
                f"baseline contract violated for task {bundle.spec.id} "
                f"({len(problems)} problem(s) above)."
            )
        state = bundle.load_state()
        state.status = "validated"
        state.validated_at = utc_now_iso()
        state.image_tag = image.tag
        state.image_digest = docker.image_id(image.tag)
        bundle.save_state(state)
        rec.log(f"baseline contract holds ({len(results)} tests x{attempts})")
        console.print(
            f"[green]Baseline contract holds[/green] for [bold]{bundle.spec.id}[/bold]: "
            f"all pass2pass pass, all fail2pass fail (x{attempts} consistent)."
        )


def _make_solver(
    name: str,
    bundle: Bundle,
    patch: Path | None,
    gold: bool,
    model: str | None,
    max_iterations: int,
) -> Solver:
    """Resolve --solver/--patch/--gold/--model flags into a Solver instance."""
    if name == "claude":
        if patch or gold:
            raise TaskError("--patch/--gold only apply to the stub solver.")
        return ClaudeSolver(model=model, max_iterations=max_iterations)
    if name != "stub":
        raise TaskError(f"Unknown solver {name!r}. Available: stub, claude.")
    if patch and gold:
        raise TaskError("Pass either --patch or --gold, not both.")
    if gold:
        patch = bundle.gold_patch_path
        if not patch.is_file():
            raise TaskError(
                f"--gold requires a golden patch at {patch}, but none exists. "
                "Add patch.diff to the bundle or pass --patch <file>."
            )
    if patch and not patch.is_file():
        raise TaskError(f"Patch file {patch} does not exist.")
    return StubSolver(patch)


@app.command()
def run(
    bundle_path: Annotated[Path, typer.Argument(help="Bundle directory to run a solver on.")],
    solver: Annotated[
        str, typer.Option(help='Solver to use: "stub" (deterministic) or "claude" (LLM).')
    ] = "stub",
    model: Annotated[
        str | None,
        typer.Option(help="Model for the claude solver (default: $ANTHROPIC_MODEL or opus)."),
    ] = None,
    max_iterations: Annotated[
        int, typer.Option(help="Iteration cap for the claude solver's agent loop.")
    ] = 30,
    patch: Annotated[
        Path | None, typer.Option(help="Patch the stub solver applies to the workspace.")
    ] = None,
    gold: Annotated[
        bool, typer.Option(help="Shorthand: stub applies the bundle's golden patch.diff.")
    ] = False,
    rebuild: Annotated[bool, typer.Option(help="Force an image rebuild even if cached.")] = False,
) -> None:
    """Run a solver against the task, then grade it with the hidden tests.

    Two-phase: the solver works in place in a container with no hidden tests
    (verified by a content leak guard), then its changeset is replayed into a fresh
    evaluation container and graded.
    Verdict is RESOLVED only if all fail2pass tests now pass AND all pass2pass tests
    still pass. Emits a JSON report and records the run in SQLite.
    """
    with record_command("run", bundle_path) as rec:
        bundle = Bundle.load(bundle_path)
        bundle.test_format()
        solver_obj = _make_solver(solver, bundle, patch, gold, model, max_iterations)
        docker = Docker()
        docker.ensure_available()
        with console.status("Ensuring task image..."):
            image, build_log = ensure_image(docker, bundle, rebuild=rebuild)
        if build_log:
            rec.save_artifact("build_log", "image_build.log", build_log)

        run_id = new_id("run")
        versions = tool_versions(docker)
        digest = docker.image_id(image.tag)
        rec.db.insert_run(
            run_id,
            rec.command_id,
            str(bundle.spec.id),
            solver_obj.name,
            solver_obj.model,
            utc_now_iso(),
            image.tag,
            digest,
            json.dumps(versions, sort_keys=True),
        )
        rec.log(f"run {run_id} started (solver={solver_obj.name})")
        try:
            with console.status("Running baseline -> solve -> grade phases..."):
                outcome = execute_run(docker, bundle, image, solver_obj)
        except BaseException:
            rec.db.finish_run(run_id, "ERROR", utc_now_iso())
            rec.log(f"run {run_id} errored")
            raise
        rec.db.record_test_results(rec.command_id, "baseline", outcome.baseline, run_id=run_id)
        rec.db.record_test_results(
            rec.command_id, "post_solver", outcome.post_solver, run_id=run_id
        )
        rec.db.finish_run(
            run_id,
            outcome.verdict,
            utc_now_iso(),
            outcome.solve.input_tokens,
            outcome.solve.output_tokens,
            outcome.solve.cost_usd,
        )
        rec.save_artifact("solver_diff", "solver.diff", outcome.diff)
        rec.save_artifact("solver_transcript", "transcript.txt", outcome.solve.transcript)
        rec.save_artifact(
            "test_output",
            "run_tests.txt",
            "\n".join(
                f"=== {e.test} [{e.bucket}] {phase}: {e.status} ===\n{e.output}"
                for phase, execs in (
                    ("baseline", outcome.baseline),
                    ("post_solver", outcome.post_solver),
                )
                for e in execs
            ),
        )
        report = build_report(
            run_id, rec.command_id, bundle, solver_obj, outcome, image.tag, digest, versions
        )
        report_path = rec.artifact_dir / "report.json"
        write_report(report_path, report)
        rec.db.add_artifact(rec.command_id, "report", str(report_path), run_id=run_id)

        baseline_status = {e.test: e.status for e in outcome.baseline}
        table = Table(title=f"Run {run_id}: {bundle.spec.id} ({solver_obj.name})")
        table.add_column("Test")
        table.add_column("Bucket")
        table.add_column("Before")
        table.add_column("After")
        for r in outcome.results:
            after_ok = r.status == "passed"
            style = "green" if after_ok else "red"
            table.add_row(
                r.test,
                r.bucket,
                baseline_status.get(r.test, "?"),
                f"[{style}]{r.status}[/{style}]",
            )
        console.print(table)
        changes = outcome.changes
        console.print(
            f"solver changed {len(changes.modified)} file(s), added {len(changes.added)}, "
            f"deleted {len(changes.deleted)}"
        )
        verdict_style = "bold green" if outcome.verdict == "RESOLVED" else "bold red"
        console.print(f"verdict: [{verdict_style}]{outcome.verdict}[/{verdict_style}]")
        rec.log(f"run {run_id} verdict: {outcome.verdict}")
        console.print(f"report: {report_path}")
        console.print(f"[dim]run id: {run_id} (task runs show {run_id})[/dim]")


@app.command("verify-gold")
def verify_gold(
    bundle_path: Annotated[Path, typer.Argument(help="Bundle directory to verify.")],
    rebuild: Annotated[bool, typer.Option(help="Force an image rebuild even if cached.")] = False,
) -> None:
    """Prove the task is solvable: apply patch.diff and confirm fail2pass flips, pass2pass holds.

    Runs the same baseline -> apply -> grade pipeline as `task run`, driving a
    deterministic stub solver with the bundle's golden patch. It is an authoring
    check (sibling to `validate`), so it records no solver run — only a command with
    its per-phase test results. Exits 2 if the golden patch does not cleanly resolve
    the task, naming each fail2pass test it fails to flip and each pass2pass it breaks.
    """
    with record_command("verify-gold", bundle_path) as rec:
        bundle = Bundle.load(bundle_path)
        bundle.test_format()
        gold = bundle.gold_patch_path
        if not gold.is_file():
            raise TaskError(
                f"No golden patch at {gold}. `verify-gold` needs a patch.diff in the bundle "
                "(import-swebench writes one; otherwise add it by hand)."
            )
        docker = Docker()
        docker.ensure_available()
        with console.status("Ensuring task image..."):
            image, build_log = ensure_image(docker, bundle, rebuild=rebuild)
        if build_log:
            rec.save_artifact("build_log", "image_build.log", build_log)
        with console.status("Running baseline -> apply gold -> grade..."):
            outcome = execute_run(docker, bundle, image, StubSolver(gold))

        rec.add_test_results("baseline", outcome.baseline)
        rec.add_test_results("post_gold", outcome.post_solver)
        rec.save_artifact(
            "test_output",
            "verify_gold_tests.txt",
            "\n".join(
                f"=== {e.test} [{e.bucket}] {phase}: {e.status} ===\n{e.output}"
                for phase, execs in (
                    ("baseline", outcome.baseline),
                    ("post_gold", outcome.post_solver),
                )
                for e in execs
            ),
        )

        baseline = consolidate(outcome.baseline)
        baseline_status = {r.test: r.status for r in baseline}
        table = Table(title=f"verify-gold: {bundle.spec.id}")
        for col in ("Test", "Bucket", "Baseline", "After gold"):
            table.add_column(col)
        for r in outcome.results:
            ok = r.status == "passed"
            style = "green" if ok else "red"
            table.add_row(
                r.test, r.bucket, baseline_status.get(r.test, "?"), f"[{style}]{r.status}[/{style}]"
            )
        console.print(table)

        problems = check_gold_contract(baseline, outcome.results)
        if problems:
            for p in problems:
                rec.log(f"gold verification failed: {p}")
                console.print(f"[bold red]gold verification failed:[/bold red] {p}")
            raise ContractViolation(
                f"golden patch does not resolve task {bundle.spec.id} "
                f"({len(problems)} problem(s) above)."
            )
        rec.log(f"gold verification passed ({len(outcome.results)} tests)")
        console.print(
            f"[green]Golden patch verified[/green] for [bold]{bundle.spec.id}[/bold]: "
            "all fail2pass flip to pass, all pass2pass hold. The task is solvable."
        )


@app.command("import-swebench")
def import_swebench(
    instance_id: Annotated[str, typer.Argument(help="SWE-bench Pro instance id (HuggingFace).")],
    dest: Annotated[
        Path | None, typer.Option(help="Bundle directory to create (default: ./<instance-id>).")
    ] = None,
    test_command: Annotated[
        str | None, typer.Option(help="Override the per-language default test command.")
    ] = None,
    timeout: Annotated[int, typer.Option(help="Per-test timeout in seconds.")] = 600,
    init_after: Annotated[
        bool,
        typer.Option("--init/--no-init", help="Run task init (clone + image) after conversion."),
    ] = True,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify/--no-verify",
            help="After init, run verify-gold to confirm the imported instance is solvable.",
        ),
    ] = True,
) -> None:
    """Convert a ScaleAI/SWE-bench_Pro instance into a ready-to-validate bundle.

    Uses the instance's prebuilt Docker image (jefzda/sweap-images) as the base, the
    test patch + explicit fail2pass/pass2pass ids as hidden tests, and the gold
    patch as patch.diff. With --init (default) the bundle is built and then
    verify-gold runs: a non-gradeable instance — e.g. one whose package is not an
    editable install, so the solver's edits wouldn't be imported — fails loudly here
    instead of silently grading every solver UNRESOLVED later.
    """
    bundle_dir = dest or Path(instance_id)
    with record_command("import-swebench", bundle_dir) as rec:
        console.print(f"Fetching [bold]{instance_id}[/bold] from HuggingFace ...")
        row = fetch_instance(instance_id)
        bundle = convert_instance(row, bundle_dir, test_command=test_command, timeout=timeout)
        rec.log(f"converted {instance_id} -> {bundle.path}")
        console.print(
            f"[green]Converted[/green] to bundle [bold]{bundle.path}[/bold] "
            f"({len(bundle.spec.tests.fail2pass_ids)} fail2pass, "
            f"{len(bundle.spec.tests.pass2pass_ids)} pass2pass tests, "
            f"base image {bundle.spec.environment.base_image.split(':')[0]}:...)"
        )
    if not init_after:
        console.print(f"Next: task init {bundle_dir} && task verify-gold {bundle_dir}")
        return
    init(bundle_dir)
    if verify:
        console.print("Confirming the golden patch resolves the task (verify-gold) ...")
        verify_gold(bundle_dir)


@app.command()
def logs(
    command_id: Annotated[
        str | None, typer.Argument(help="Command id to inspect; omit to list recent commands.")
    ] = None,
    limit: Annotated[int, typer.Option(help="How many recent commands to list.")] = 20,
) -> None:
    """Show the log, test results, and artifacts recorded for a CLI command."""
    db = Database(settings.db_path)
    try:
        if command_id is None:
            rows = db.recent_commands(limit)
            if not rows:
                console.print(f"No commands recorded yet in {settings.db_path}.")
                return
            table = Table(title=f"Recent commands ({settings.db_path})")
            for col in ("id", "name", "exit", "started at", "bundle"):
                table.add_column(col)
            for row in rows:
                exit_code = row["exit_code"]
                style = "green" if exit_code == 0 else "red"
                table.add_row(
                    row["id"],
                    row["name"],
                    f"[{style}]{exit_code}[/{style}]" if exit_code is not None else "?",
                    row["started_at"],
                    row["bundle_path"] or "-",
                )
            console.print(table)
            return

        cmd = db.get_command(command_id)
        if cmd is None:
            raise TaskError(
                f"No command {command_id!r} in {settings.db_path}. "
                "Run `task logs` (no argument) to list recent command ids."
            )
        console.print(f"[bold]{cmd['name']}[/bold] {cmd['id']}")
        console.print(f"  argv:     {' '.join(json.loads(cmd['argv']))}")
        console.print(f"  bundle:   {cmd['bundle_path'] or '-'}")
        console.print(f"  started:  {cmd['started_at']}")
        console.print(f"  finished: {cmd['finished_at']} (exit {cmd['exit_code']})")

        test_rows = db.test_results_for(command_id=command_id)
        if test_rows:
            table = Table(title="Test results")
            for col in ("test", "bucket", "phase", "attempt", "status", "duration (s)"):
                table.add_column(col)
            for t in test_rows:
                style = "green" if t["status"] == "passed" else "red"
                table.add_row(
                    t["test_name"],
                    t["bucket"],
                    t["phase"],
                    str(t["attempt"]),
                    f"[{style}]{t['status']}[/{style}]",
                    f"{t['duration_seconds']:.2f}",
                )
            console.print(table)

        for art in db.artifacts_for(command_id):
            console.print(f"  artifact ({art['type']}): {art['path']}")
        log_path = Path(cmd["log_path"]) if cmd["log_path"] else None
        if log_path and log_path.is_file():
            console.print(f"\n[bold]command.log[/bold] ({log_path}):")
            console.print(log_path.read_text().rstrip() or "(empty)")
    finally:
        db.close()


@app.command()
def diff(
    run_id: Annotated[str, typer.Argument(help="Run id whose solver patch to print.")],
) -> None:
    """Print the unified diff a run's solver produced (raw, pipeable to `git apply`)."""
    db = Database(settings.db_path)
    try:
        run = db.get_run(run_id)
        if run is None:
            raise TaskError(
                f"No run {run_id!r} in {settings.db_path}. Use `task runs list` to see ids."
            )
        artifact = next(
            (a for a in db.artifacts_for(run["command_id"]) if a["type"] == "solver_diff"), None
        )
        if artifact is None:
            raise TaskError(
                f"Run {run_id} has no stored diff (it likely errored before the solve phase)."
            )
        diff_text = Path(artifact["path"]).read_text()
    finally:
        db.close()
    if diff_text.strip():
        console.print(diff_text, markup=False, highlight=False, soft_wrap=True, end="")
    else:
        console.print(f"[dim]Run {run_id} produced no changes (empty diff).[/dim]")


@app.command()
def clean(
    bundle_path: Annotated[
        Path | None, typer.Argument(help="Bundle to clean (its image + workspace clone).")
    ] = None,
    run_id: Annotated[
        str | None, typer.Option("--run", help="Remove a single run's artifacts directory.")
    ] = None,
    all_: Annotated[
        bool, typer.Option("--all", help="Remove ALL task-bundle images and the artifacts dir.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Reclaim disk: remove task-bundle images, workspace clones, and run artifacts.

    Exactly one target: a bundle path, --run <id>, or --all. Always previews what it
    will delete and prompts for confirmation unless --yes is given. The SQLite log is
    never touched, so `task logs`/`task runs` history survives a clean.
    """
    if sum([bundle_path is not None, run_id is not None, all_]) != 1:
        raise TaskError("Pass exactly one of: a bundle path, --run <id>, or --all.")

    docker = Docker()
    images: list[str] = []
    dirs: list[Path] = []

    if all_:
        images = docker.list_images(f"{IMAGE_REPO}/")
        if settings.artifacts_dir.exists():
            dirs = [settings.artifacts_dir]
    elif run_id is not None:
        db = Database(settings.db_path)
        try:
            run = db.get_run(run_id)
            if run is None:
                raise TaskError(
                    f"No run {run_id!r} in {settings.db_path}. Use `task runs list` to see ids."
                )
            command_dir = settings.artifacts_dir / run["command_id"]
        finally:
            db.close()
        if command_dir.exists():
            dirs = [command_dir]
    else:
        assert bundle_path is not None
        bundle = Bundle.load(bundle_path)
        images = docker.list_images(f"{IMAGE_REPO}/{bundle.spec.id}:")
        if bundle.workspace_dir.exists():
            dirs = [bundle.workspace_dir]

    if not images and not dirs:
        console.print("Nothing to remove.")
        return

    console.print("[bold]Will remove:[/bold]")
    for image in images:
        console.print(f"  image     {image}")
    for directory in dirs:
        console.print(f"  directory {directory}")
    if not yes and not typer.confirm("Proceed?"):
        raise typer.Abort()

    for image in images:
        docker.rmi(image)
    for directory in dirs:
        shutil.rmtree(directory, ignore_errors=True)

    if bundle_path is not None:
        bundle = Bundle.load(bundle_path)
        state = bundle.load_state()
        state.status = "scaffolded"
        state.image_tag = None
        state.image_digest = None
        bundle.save_state(state)

    console.print(f"[green]Removed[/green] {len(images)} image(s) and {len(dirs)} director(y/ies).")


_DISK_WARN_GB = 5.0


def _existing_ancestor(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return Path("/")


def _check_docker() -> tuple[str, str, str]:
    docker = Docker()
    try:
        docker.ensure_available()
    except DockerError as e:
        return ("Docker daemon", "fail", str(e))
    return ("Docker daemon", "ok", f"server {docker.version()}")


def _check_git() -> tuple[str, str, str]:
    try:
        proc = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return ("git", "fail", "git not found on PATH")
    if proc.returncode != 0:
        return ("git", "fail", "git not found on PATH")
    return ("git", "ok", proc.stdout.strip())


def _check_api_key() -> tuple[str, str, str]:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ("ANTHROPIC_API_KEY", "ok", "set")
    return ("ANTHROPIC_API_KEY", "warn", "unset — needed only for `task run --solver claude`")


def _check_disk() -> tuple[str, str, str]:
    free_gb = shutil.disk_usage(_existing_ancestor(settings.artifacts_dir)).free / 1e9
    status = "warn" if free_gb < _DISK_WARN_GB else "ok"
    return ("Disk space", status, f"{free_gb:.1f} GB free (task images are large)")


@app.command()
def doctor() -> None:
    """Preflight environment checks. Exits 1 if a required dependency (Docker, git) is missing."""
    results = [_check_docker(), _check_git(), _check_api_key(), _check_disk()]
    styles = {"ok": "green", "warn": "yellow", "fail": "red"}
    table = Table(title="task doctor")
    for col in ("Check", "Status", "Detail"):
        table.add_column(col)
    for name, status, detail in results:
        table.add_row(name, f"[{styles[status]}]{status.upper()}[/{styles[status]}]", detail)
    console.print(table)
    failures = [name for name, status, _ in results if status == "fail"]
    if failures:
        raise TaskError(
            f"doctor found {len(failures)} blocking problem(s): {', '.join(failures)}. "
            "Fix the FAIL rows above before running tasks."
        )
    console.print("[green]All required checks passed.[/green]")


@runs_app.command("list")
def runs_list(
    limit: Annotated[int, typer.Option(help="How many runs to list.")] = 50,
) -> None:
    """List past solver runs (newest first)."""
    db = Database(settings.db_path)
    try:
        rows = db.list_runs(limit)
        if not rows:
            console.print(
                f"No runs recorded yet in {settings.db_path}. Runs are created by `task run`."
            )
            return
        table = Table(title="Solver runs")
        for col in ("id", "task", "solver", "model", "verdict", "started at"):
            table.add_column(col)
        for row in rows:
            verdict = row["verdict"] or "?"
            style = "green" if verdict == "RESOLVED" else "red"
            table.add_row(
                row["id"],
                row["task_id"],
                row["solver"],
                row["model"] or "-",
                f"[{style}]{verdict}[/{style}]",
                row["started_at"],
            )
        console.print(table)
    finally:
        db.close()


@runs_app.command("show")
def runs_show(run_id: Annotated[str, typer.Argument(help="Run id to inspect.")]) -> None:
    """Show one run: metadata, per-phase test results, and artifacts."""
    db = Database(settings.db_path)
    try:
        row = db.get_run(run_id)
        if row is None:
            raise TaskError(
                f"No run {run_id!r} in {settings.db_path}. Use `task runs list` to see ids."
            )
        console.print(f"[bold]run[/bold] {row['id']} (command {row['command_id']})")
        for key in ("task_id", "solver", "model", "verdict", "started_at", "finished_at"):
            console.print(f"  {key}: {row[key] or '-'}")
        if row["cost_usd"] is not None:
            console.print(
                f"  tokens: {row['input_tokens']} in / {row['output_tokens']} out "
                f"(${row['cost_usd']:.4f})"
            )
        console.print(f"  image: {row['image_tag']} ({row['image_digest']})")

        test_rows = db.test_results_for(run_id=run_id)
        if test_rows:
            table = Table(title="Test results")
            for col in ("test", "bucket", "phase", "attempt", "status"):
                table.add_column(col)
            for t in test_rows:
                style = "green" if t["status"] == "passed" else "red"
                table.add_row(
                    t["test_name"],
                    t["bucket"],
                    t["phase"],
                    str(t["attempt"]),
                    f"[{style}]{t['status']}[/{style}]",
                )
            console.print(table)
    finally:
        db.close()


def main() -> None:
    """Console-script entrypoint with uniform TaskError rendering."""
    try:
        app(standalone_mode=False)
    except TaskError as e:
        err_console.print(f"error: {e}")
        raise SystemExit(e.exit_code) from None
    except typer.Exit as e:
        raise SystemExit(e.exit_code) from None
    except typer.Abort:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
