"""Command line interface."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from cba_bot.catalog import corpus_stats, discover_contracts
from cba_bot.config import (
    load_settings,
    mask_key,
    resolve_api_key,
    update_saved_settings,
)
from cba_bot.index import Embedder, Store, index_contracts
from cba_bot.llm import LLMError, list_models
from cba_bot.runtime import Runtime

app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    help="Collective-bargaining-agreement research with recency-first retrieval.",
)
console = Console()


def _settings(contracts: Path | None, data_dir: Path | None, model: str | None):
    return load_settings(
        contracts_dir=str(contracts) if contracts else None,
        data_dir=str(data_dir) if data_dir else None,
        model=model,
    )


@app.callback()
def default(
    ctx: typer.Context,
    contracts: Path | None = typer.Option(
        None, "--contracts", "-c", help="Folder holding the CBA corpus."
    ),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Index location."),
    model: str | None = typer.Option(None, "--model", help="OpenCode Go model."),
) -> None:
    """Launch the TUI when no subcommand is given."""
    if ctx.invoked_subcommand is not None:
        return
    settings = _settings(contracts, data_dir, model)
    from cba_bot.tui import CBAApp

    runtime = Runtime.build(settings)
    try:
        CBAApp(runtime).run()
    finally:
        runtime.close()


@app.command()
def chat(
    contracts: Path | None = typer.Option(None, "--contracts", "-c"),
    data_dir: Path | None = typer.Option(None, "--data-dir"),
    model: str | None = typer.Option(None, "--model"),
) -> None:
    """Open the CBA Desk TUI."""
    settings = _settings(contracts, data_dir, model)
    from cba_bot.tui import CBAApp

    runtime = Runtime.build(settings)
    try:
        CBAApp(runtime).run()
    finally:
        runtime.close()


@app.command()
def ask(
    question: str = typer.Argument(..., help="Question or clause request."),
    contracts: Path | None = typer.Option(None, "--contracts", "-c"),
    data_dir: Path | None = typer.Option(None, "--data-dir"),
    model: str | None = typer.Option(None, "--model"),
    fresh: bool = typer.Option(
        False, "--fresh", help="Use only current/upcoming agreements."
    ),
    sector: str = typer.Option("all", "--sector"),
    state: str = typer.Option("all", "--state"),
    show_sources: bool = typer.Option(True, "--sources/--no-sources"),
) -> None:
    """Answer one question in the terminal."""
    settings = _settings(contracts, data_dir, model)
    runtime = Runtime.build(settings)
    try:
        asyncio.run(
            _ask(
                runtime,
                question,
                fresh,
                sector,
                state.upper() if state.lower() != "all" else "all",
                show_sources,
            )
        )
    finally:
        runtime.close()


async def _ask(runtime, question, fresh, sector, state, show_sources) -> None:
    from cba_bot.index import retrieve_sources
    from cba_bot.prompt import build_messages

    sources = await asyncio.to_thread(
        retrieve_sources,
        runtime.store,
        runtime.embedder,
        question,
        top_k=runtime.settings.top_k,
        current_only=fresh,
        sector=sector,
        jurisdiction=state,
    )
    if not sources:
        console.print("[yellow]No matching contracts.[/] Try a broader phrase, "
                      "or build the corpus with `cba-bot scrape && cba-bot index`.")
        raise typer.Exit(1)
    if show_sources:
        console.print("[bold]Sources[/]")
        for source in sources:
            contract = source.contract
            color = {"current": "green", "expired": "red"}.get(
                contract.status, "yellow"
            )
            console.print(
                f"  [bold]S{source.index}[/] [{color}]{contract.status.upper()}[/] "
                f"{contract.term} · {escape(contract.title)} "
                f"[dim]({escape(contract.union_name or 'union n/a')})[/]"
            )
        console.print()
    messages = build_messages(question, sources)
    console.print("[bold #d9a441]CBA Desk[/]\n")
    try:
        async for token in runtime.llm.stream(messages):
            console.print(token, end="", soft_wrap=True, highlight=False, markup=False)
    except LLMError as exc:
        console.print(f"\n[red]{exc}[/]")
        raise typer.Exit(1) from exc
    console.print("\n\n[dim]Contract research, not legal advice.[/]")


def _select_corpus(corpus, limit: int):
    """Always keep the local folder; round-robin the newest scraped sources.

    Taking the global newest N would fill the index with OPM/OLMS agreements
    that happen to expire in the 2030s.  Round-robin keeps federal, state,
    municipal, education, and healthcare contracts represented while still
    taking the newest documents within each source.
    """
    local = [c for c in corpus if c.source == "local"]
    buckets: dict[str, list] = {}
    for contract in corpus:
        if contract.source != "local":
            buckets.setdefault(contract.source, []).append(contract)
    for bucket in buckets.values():
        bucket.sort(key=lambda c: c.recency_sort_key(), reverse=True)
    selected = []
    depth = 0
    while len(selected) < limit:
        added = False
        for name in sorted(buckets):
            bucket = buckets[name]
            if depth < len(bucket):
                selected.append(bucket[depth])
                added = True
                if len(selected) >= limit:
                    break
        if not added:
            break
        depth += 1
    return sorted(local, key=lambda c: c.recency_sort_key(), reverse=True) + sorted(
        selected, key=lambda c: c.recency_sort_key(), reverse=True
    )


@app.command()
def index(
    contracts: Path | None = typer.Option(None, "--contracts", "-c"),
    data_dir: Path | None = typer.Option(None, "--data-dir"),
    rebuild: bool = typer.Option(False, "--rebuild", help="Drop and rebuild the index."),
    limit: int | None = typer.Option(
        None, "--limit", help="Index only the N most recent contracts."
    ),
    ocr: bool = typer.Option(
        True, "--ocr/--no-ocr", help="OCR scanned pages (slower)."
    ),
    ocr_pages: int = typer.Option(
        30, "--ocr-pages", help="Maximum OCR pages per document."
    ),
    ocr_seconds: float = typer.Option(
        45.0, "--ocr-seconds", help="OCR time budget per document."
    ),
) -> None:
    """Extract, chunk, and embed every contract in the corpus."""
    settings = _settings(contracts, data_dir, None)
    corpus = discover_contracts(settings.contracts_path())
    if limit:
        corpus = _select_corpus(corpus, limit)
    if not corpus:
        console.print(
            f"[yellow]No PDFs or DOCX files under {settings.contracts_path()}[/]"
        )
        raise typer.Exit(1)
    if rebuild and limit:
        console.print(
            "[yellow]--rebuild with --limit drops every indexed contract "
            "outside the selection.[/]"
        )
    store = Store(settings.data_path() / "index")
    embedder = Embedder(settings.embed_model)
    console.print(
        f"Indexing [bold]{len(corpus):,}[/] contracts from "
        f"[cyan]{settings.contracts_path()}[/]"
    )
    with Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("extracting + embedding", total=len(corpus))
        last = {"label": ""}

        def update(done: int, total: int, label: str) -> None:
            last["label"] = label
            progress.update(task, completed=done, description=f"[dim]{escape(label[:60])}[/]")

        try:
            summary = index_contracts(
                corpus,
                store,
                embedder,
                rebuild=rebuild,
                progress=update,
                ocr=ocr,
                ocr_pages=ocr_pages,
                ocr_seconds=ocr_seconds,
                prune=limit is None,
            )
        finally:
            store.close()
    console.print(
        f"\n[green]Added {summary['added']:,}[/] · "
        f"skipped {summary['skipped']:,} unchanged · "
        f"[red]{summary['failed']:,} failed[/]"
    )
    if summary["failed"]:
        console.print("[dim]Failed files are usually scanned PDFs with no usable text.[/]")


@app.command()
def scrape(
    source: str = typer.Option(
        "all", "--source", "-s", help="cornell|escholarship|olms|opm|ny|states|all"
    ),
    contracts: Path | None = typer.Option(None, "--contracts", "-c"),
    limit: int = typer.Option(
        150, "--limit", help="Newest documents per source (per collection for Cornell)."
    ),
    full: bool = typer.Option(False, "--full", help="Download every listed document."),
    concurrency: int = typer.Option(6, "--concurrency"),
) -> None:
    """Download CBAs from the public source directories into contracts/scraped/."""
    from cba_bot.ingest import SOURCES

    settings = _settings(contracts, None, None)
    chosen = sorted(SOURCES) if source == "all" else [s.strip() for s in source.split(",")]
    unknown = [name for name in chosen if name not in SOURCES]
    if unknown:
        console.print(f"[red]Unknown source(s): {', '.join(unknown)}[/]")
        raise typer.Exit(1)

    for name in chosen:
        console.print(f"\n[bold #d9a441]{name}[/] → [cyan]{settings.contracts_path()}[/]")

        def progress(label, ok, failed, total, _name=name):
            console.print(
                f"  [dim]{label}[/]: [green]{ok}[/] downloaded, "
                f"[red]{failed}[/] failed / {total} queued"
            )

        try:
            summary = SOURCES[name](
                settings.contracts_path(),
                limit=limit,
                full=full,
                concurrency=concurrency,
                progress=progress,
            )
        except Exception as exc:
            console.print(f"  [red]{name} failed: {exc}[/]")
            continue
        console.print(
            f"  [bold]done[/] — {summary.get('downloaded', 0):,} files; "
            f"{summary.get('listed', 0):,} listed at the source."
        )
    console.print("\nNext: [bold]cba-bot index[/]")


@app.command()
def compact(
    data_dir: Path | None = typer.Option(None, "--data-dir"),
) -> None:
    """Drop orphaned vectors left by interrupted or repeated indexing runs."""
    settings = _settings(None, data_dir, None)
    store = Store(settings.data_path() / "index")
    try:
        removed = store.compact_vectors()
    finally:
        store.close()
    console.print(f"Removed {removed:,} orphaned vectors.")


@app.command()
def stats(
    contracts: Path | None = typer.Option(None, "--contracts", "-c"),
    data_dir: Path | None = typer.Option(None, "--data-dir"),
) -> None:
    """Show corpus and index counts."""
    settings = _settings(contracts, data_dir, None)
    corpus = discover_contracts(settings.contracts_path())
    corpus_info = corpus_stats(corpus)
    store = Store(settings.data_path() / "index")
    try:
        index_info = store.stats()
    finally:
        store.close()

    console.print(f"[bold #d9a441]Corpus[/] {settings.contracts_path()}")
    console.print(f"  files: {corpus_info['contracts']:,}")
    for key in ("status", "sector", "jurisdiction", "sources"):
        line = ", ".join(f"{k} {v}" for k, v in corpus_info[key].items())
        console.print(f"  {key}: {line}")

    console.print(f"\n[bold #d9a441]Index[/] {index_info['data_dir']}")
    console.print(
        f"  documents {index_info['documents']:,} · chunks {index_info['chunks']:,} · "
        f"vectors {index_info['vectors']:,} ({index_info['vector_dim']}d)"
    )
    table = Table(show_header=True, header_style="bold #d9a441")
    table.add_column("status")
    table.add_column("documents", justify="right")
    for key, value in index_info["status"].items():
        table.add_row(key, f"{value:,}")
    console.print(table)


@app.command()
def models(
    model_filter: str | None = typer.Option(None, "--filter"),
) -> None:
    """List chat models available from OpenCode Go."""
    settings = load_settings()
    key = resolve_api_key(settings)
    try:
        names = asyncio.run(list_models(settings, api_key=key))
    except Exception as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    for name in names:
        if model_filter and model_filter.lower() not in name.lower():
            continue
        marker = " [green]← current[/]" if name == settings.resolved_model() else ""
        console.print(f"{name}{marker}")


@app.command()
def config(
    set_key: str | None = typer.Option(None, "--set-key", help="Save API key."),
    set_model: str | None = typer.Option(None, "--set-model"),
    set_data_dir: Path | None = typer.Option(None, "--set-data-dir"),
    set_contracts_dir: Path | None = typer.Option(None, "--set-contracts-dir"),
    show: bool = typer.Option(False, "--show"),
) -> None:
    """Inspect or update saved settings."""
    settings = load_settings()
    updates = {}
    if set_key:
        updates["api_key"] = set_key
    if set_model:
        updates["model"] = set_model
    if set_data_dir:
        updates["data_dir"] = str(set_data_dir.resolve())
    if set_contracts_dir:
        updates["contracts_dir"] = str(set_contracts_dir.resolve())
    if updates:
        settings = update_saved_settings(**updates)
        from cba_bot.config import config_path

        console.print(f"Saved to {config_path()}")
    if show or not updates:
        console.print(f"provider:   {settings.provider}")
        console.print(f"model:      {settings.resolved_model()}")
        console.print(f"base URL:   {settings.resolved_base_url()}")
        console.print(f"api key:    {mask_key(settings.api_key)}")
        console.print(f"corpus:     {settings.contracts_path()}")
        console.print(f"data:       {settings.data_path()}")
        console.print(f"embeddings: {settings.embed_model}")
