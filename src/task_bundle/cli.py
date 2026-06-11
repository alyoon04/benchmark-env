"""Typer CLI entrypoint. Thin layer: parse args, delegate to library code, render output.

Every command will additionally log to SQLite from milestone 3 onward.
"""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from task_bundle import __version__
from task_bundle.bundle import Bundle, utc_now_iso
from task_bundle.errors import TaskError
from task_bundle.workspace import clone_at_commit

app = typer.Typer(
    name="task",
    help="Package, validate, and run LLM solvers against SWE-bench-style task bundles.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
console = Console()
err_console = Console(stderr=True, style="bold red")


@app.callback()
def _root(
    version: Annotated[bool, typer.Option("--version", help="Show version and exit.")] = False,
) -> None:
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
) -> None:
    """Scaffold the bundle (if needed) and materialize the repo at the pinned commit.

    Two modes: with --repo/--commit on a fresh directory, scaffolds task.json,
    description.md, and the tests/ skeleton first; on an existing bundle, just
    (re-)initializes the workspace from task.json.
    """
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
                f"  task init {bundle_path} --repo https://github.com/org/repo --commit <full-sha>"
            )
        bundle = Bundle.scaffold(
            bundle_path,
            repo_url=repo,
            commit=commit,
            base_image=base_image,
            test_command=test_command,
            setup_commands=setup,
        )
        console.print(f"[green]Scaffolded[/green] bundle at [bold]{bundle.path}[/bold]")

    console.print(
        f"Cloning [bold]{bundle.spec.repo.url}[/bold] @ {bundle.spec.repo.commit[:12]} ..."
    )
    clone_at_commit(
        bundle.spec.repo.url, bundle.spec.repo.commit, bundle.workspace_dir, force=force
    )
    state = bundle.load_state()
    state.status = "initialized"
    state.initialized_at = utc_now_iso()
    bundle.save_state(state)
    console.print(
        f"[green]Initialized[/green] task [bold]{bundle.spec.id}[/bold] "
        f"(workspace pinned to {bundle.spec.repo.commit[:12]}).\n"
        "Next: add hidden tests under tests/fail2pass/ and tests/pass2pass/, "
        "then run [bold]task validate[/bold]."
    )


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
