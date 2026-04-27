"""SyncCore CLI - run, status, and reset commands."""

from __future__ import annotations

import signal
import socket
import sys
import threading
import time
from pathlib import Path

import typer
import uvicorn
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from config import Settings, __version__, bootstrap_env, get_app_dir
from utils.certs import ensure_certs
from utils.logging import get_logger, setup_logging

console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"SyncCore {__version__}")
        raise typer.Exit()


cli = typer.Typer(
    help="SyncCore - one-way client/server file sync",
    no_args_is_help=False,
    pretty_exceptions_enable=True,
)


@cli.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show version and exit",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """SyncCore - one-way client/server file sync."""


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def _boot(quiet: bool = False) -> tuple[Settings, "Database", "SyncIgnore", bool]:
    """Bootstrap the environment: load settings, create DB, parse ignore rules."""
    from utils.file_index import Database
    from utils.filters import SyncIgnore

    first_run = bootstrap_env()
    settings = Settings()
    settings.ensure_folders()

    if not quiet:
        setup_logging(settings.log_level, str(Path(settings.db_path).parent))

    generated_certs = ensure_certs(settings.ssl_cert, settings.ssl_key)

    db = Database(settings.db_path)
    ignore = SyncIgnore(settings.syncignore_path)
    return settings, db, ignore, first_run or generated_certs


def _mode_label(run_server: bool, run_client: bool) -> str:
    if run_server and run_client:
        return "server + client"
    if run_server:
        return "server only"
    return "client only"


def _print_banner(
    settings: Settings,
    first_run: bool,
    run_server: bool,
    run_client: bool,
) -> None:
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold cyan", min_width=14)
    table.add_column()
    table.add_row("Mode", _mode_label(run_server, run_client))
    table.add_row("Sync folder", settings.sync_folder)
    if run_server:
        table.add_row("Server", f"https://localhost:{settings.port}")
    if run_client:
        table.add_row("Target", settings.server_url)
    table.add_row("Node ID", settings.node_id)

    title = "[bold green]SyncCore is running[/bold green]"
    if first_run:
        title += "  [yellow](first run - config auto-generated)[/yellow]"

    console.print()
    console.print(Panel(table, title=title, border_style="green", expand=False))
    console.print()
    console.print("  [dim]Press[/dim] [bold]Ctrl+C[/bold] [dim]to stop.[/dim]")
    console.print()


@cli.command()
def run(
    server_only: bool = typer.Option(
        False, "--server", help="Run only the central sync server"
    ),
    client_only: bool = typer.Option(
        False, "--client", help="Run only the watching sync client"
    ),
) -> None:
    """Start SyncCore (default: both server and client)."""
    from core.client import SyncClient
    from core.engine import SyncEngine
    from core.orchestrator import Orchestrator
    from core.queue_worker import QueueWorker
    from core.server import app as fastapi_app
    from core.watcher import FileWatcher

    if server_only and client_only:
        console.print(
            "\n  [bold red]Error:[/bold red] Choose either --server or --client, not both.\n"
        )
        raise SystemExit(1)

    run_server = not client_only
    run_client = not server_only

    settings, db, ignore, first_run = _boot()
    log = get_logger("main")

    if run_server and not _port_available(settings.port):
        console.print(
            f"\n  [bold red]Error:[/bold red] Port {settings.port} is already in use.\n"
            f"  Change PORT in your .env or stop the other process.\n"
        )
        raise SystemExit(1)

    stop_event = threading.Event()
    watcher = None
    worker = None
    client = None
    uvi_server = None
    srv_thread = None

    if run_server:
        fastapi_app.state.settings = settings
        fastapi_app.state.db = db

        ssl_key = settings.ssl_key if Path(settings.ssl_key).is_file() else None
        ssl_cert = settings.ssl_cert if Path(settings.ssl_cert).is_file() else None

        uvi_config = uvicorn.Config(
            fastapi_app,
            host="0.0.0.0",
            port=settings.port,
            ssl_keyfile=ssl_key,
            ssl_certfile=ssl_cert,
            log_level="warning",
        )
        uvi_server = uvicorn.Server(uvi_config)

        def _serve():
            uvi_server.run()

        srv_thread = threading.Thread(target=_serve, daemon=True, name="uvicorn")
        srv_thread.start()
        time.sleep(1)

    if run_client:
        client = SyncClient(settings)
        engine = SyncEngine(settings, db, ignore)
        engine.initial_scan()

        worker = QueueWorker(db, client, settings)
        worker.start()

        watcher = FileWatcher(settings, db, ignore)
        watcher.start()

        log.info("Watching %s and syncing to %s", settings.sync_folder, settings.server_url)

    orchestrator = Orchestrator(
        settings=settings,
        db=db,
        ignore=ignore,
        watcher=watcher,
        queue_worker=worker,
        client=client,
    )

    _print_banner(settings, first_run, run_server, run_client)

    def _shutdown(*_):
        console.print("\n  [yellow]Shutting down...[/yellow]")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=0.5)
    finally:
        if run_server and uvi_server is not None:
            uvi_server.should_exit = True
        orchestrator.stop_all()
        if run_server and srv_thread is not None:
            srv_thread.join(timeout=3)
        if db:
            db.close()


@cli.command()
def status():
    """Show current configuration and queue depth."""
    settings, db, _, _ = _boot(quiet=True)

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold cyan", min_width=14)
    table.add_column()
    table.add_row("Node ID", settings.node_id)
    table.add_row("Sync folder", settings.sync_folder)
    table.add_row("Server URL", settings.server_url)
    table.add_row("Port", str(settings.port))
    table.add_row("Indexed files", str(len(db.all_files())))
    table.add_row("Pending tasks", str(db.pending_count()))

    console.print()
    console.print(
        Panel(
            table,
            title="[bold]SyncCore Status[/bold]",
            border_style="blue",
            expand=False,
        )
    )
    console.print()


@cli.command()
def reset():
    """Delete the .env and generated certs to start fresh."""
    base = get_app_dir()
    removed = []
    for name in (".env", "cert.pem", "key.pem"):
        path = base / name
        if path.is_file():
            path.unlink()
            removed.append(name)
    if removed:
        console.print(f"  [green]Removed:[/green] {', '.join(removed)}")
        console.print("  Run [bold]python main.py run[/bold] to set up again.")
    else:
        console.print("  [dim]Nothing to remove - already clean.[/dim]")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.insert(1, "run")
    cli()
