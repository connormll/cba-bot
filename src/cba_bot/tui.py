"""CBA Desk terminal UI.

Sources sit on the left as contract cards with term and status badges; the
answer streams on the right.  Recency is the organizing principle: current
agreements are full-strength, expired ones are dimmed and penalized.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Footer, Input, Markdown, Static

from cba_bot.config import Settings
from cba_bot.index import Source
from cba_bot.llm import LLMError, list_models
from cba_bot.rag import linkify_citations, stream_answer

if TYPE_CHECKING:
    from cba_bot.runtime import Runtime

SECTORS = ("all", "public", "federal", "education", "healthcare", "private", "other")
STATUS_STYLE = {
    "current": ("#8fbf7f", "●"),
    "expired": ("#b5533c", "○"),
    "upcoming": ("#d9a441", "◐"),
    "undated": ("#7d7a6b", "·"),
}

HELP_TEXT = """\
**Commands**

- `/fresh` — toggle expired agreements (recency filter)
- `/fresh on|off` — keep only current/upcoming agreements
- `/sector public|federal|education|healthcare|private|other|all`
- `/state NY|CA|…|all` — filter by state
- `/sources` — restore the cards for the last answer
- `/open 2` — open source 2 in the contract reader
- `/model` — list OpenCode Go models · `/model <name>` to switch
- `/stats` — index and corpus counts
- `/clear` — clear the conversation
- `/help` — this message
- `Ctrl+Q` quit · `Tab` moves between contract cards

Corpus commands live in the shell: `cba-bot scrape`, `cba-bot index`, `cba-bot stats`.
"""


class SourceCard(Static, can_focus=True):
    """A clickable contract card in the shelf."""

    class Selected(Message):
        def __init__(self, answer_id: int, index: int):
            super().__init__()
            self.answer_id = answer_id
            self.index = index

    def __init__(self, source: Source, answer_id: int):
        contract = source.contract
        classes = f"source-card {contract.status}"
        super().__init__("", classes=classes)
        self.source = source
        self.answer_id = answer_id

    def render(self) -> Text:
        contract = self.source.contract
        color, glyph = STATUS_STYLE.get(contract.status, ("#7d7a6b", "·"))
        text = Text()
        text.append(f"{glyph} S{self.source.index}  ", style=f"bold {color}")
        text.append(contract.title[:30] + ("…" if len(contract.title) > 38 else ""), style="bold #e8ddc8")
        text.append("\n")
        text.append(f"{contract.term}", style=f"bold {color}")
        text.append(f"\u00a0·\u00a0{contract.status.upper()}", style=color)
        text.append("\u00a0·\u00a0")
        text.append(
            " · ".join(
                part
                for part in (
                    contract.union_name,
                    contract.sector,
                    contract.jurisdiction,
                )
                if part
            ),
            style="#9db3a4",
        )
        if self.source.chunks and self.source.chunks[0].page_start:
            text.append(f" · p. {self.source.chunks[0].page_start}", style="#7d7a6b")
        excerpt = " ".join(self.source.excerpt.split())
        if excerpt:
            text.append("\n")
            text.append(
                excerpt[:160] + ("…" if len(excerpt) > 160 else ""), style="#b9ad96"
            )
        return text

    def on_click(self) -> None:
        self.focus()
        self.post_message(self.Selected(self.answer_id, self.source.index))

    def key_enter(self) -> None:
        self.post_message(self.Selected(self.answer_id, self.source.index))


class ReaderScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+q", "app.quit", "Quit"),
    ]
    CSS = """
    Screen { background: #12100e; color: #e8ddc8; }
    #reader-head { background: #1b241f; border-bottom: thick #d9a441; padding: 1 2; }
    #reader-body { padding: 1 3; height: 1fr; }
    """

    def __init__(self, source: Source, text: str):
        super().__init__()
        self.source = source
        self.text = text

    def compose(self) -> ComposeResult:
        contract = self.source.contract
        color, glyph = STATUS_STYLE.get(contract.status, ("#7d7a6b", "·"))
        meta = Text()
        meta.append(f"{glyph} {contract.status.upper()}  ", style=f"bold {color}")
        meta.append(f"{contract.term}  ", style=f"bold {color}")
        meta.append(contract.title, style="bold #e8ddc8")
        meta.append(
            f"\n{contract.union_name or 'Union not stated'} · "
            f"{contract.employer or 'Employer not stated'} · {contract.sector}"
            + (f" · {contract.jurisdiction}" if contract.jurisdiction else ""),
            style="#9db3a4",
        )
        if contract.url:
            meta.append(f"\n{contract.url}", style="#6f8f7a")
        meta.append("\nEsc to return · Ctrl+Q to quit", style="#7d7a6b")
        yield Static(meta, id="reader-head")
        body = Text((self.text or "(no extracted text)").replace("\f", "\n\n"))
        with VerticalScroll(id="reader-body"):
            yield Static(body)

    def on_mount(self) -> None:
        self.query_one("#reader-body").focus()


class CBAApp(App):
    TITLE = "CBA Desk"
    SUB_TITLE = "contract language, newest first"
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+l", "clear_chat", "Clear"),
    ]
    CSS = """
    Screen { background: #12100e; color: #e8ddc8; }
    #masthead {
        height: 3; background: #1b241f; border-bottom: thick #d9a441;
        padding: 0 2; content-align: left middle;
    }
    #body { height: 1fr; }
    #shelf {
        width: 38%; background: #17150f; border-right: thick #3b3a2f;
    }
    #shelf-head {
        height: auto; padding: 1 2 0 2; color: #9db3a4; background: #17150f;
    }
    #sources { height: 1fr; padding: 0 1; }
    SourceCard {
        border: round #3b3a2f; border-left: thick #3b3a2f;
        background: #1d1a14; padding: 0 1; margin-bottom: 1; height: auto;
    }
    SourceCard.current { border-left: thick #8fbf7f; }
    SourceCard.expired { border-left: thick #b5533c; color: #8d8474; }
    SourceCard.upcoming { border-left: thick #d9a441; }
    SourceCard.undated { border-left: thick #7d7a6b; }
    SourceCard:focus { border: round #d9a441; border-left: thick #d9a441; background: #262117; }
    #desk { width: 1fr; }
    #chat { padding: 1 3; height: 1fr; }
    .msg { margin-bottom: 1; }
    .user-msg {
        color: #f0e6d2; background: #2a2418; border-left: thick #d9a441;
        padding: 0 1;
    }
    .brief { color: #b9ad96; }
    #prompt {
        dock: bottom; margin: 0 2 1 2; border: tall #d9a441; background: #1d1a14;
    }
    #statusbar {
        height: 1; background: #1b241f; color: #9db3a4; padding: 0 2;
    }
    Footer { background: #1b241f; color: #9db3a4; }
    """
    SOURCES_HINT = "_Contract cards appear here with their term and status._"

    def __init__(self, runtime: Runtime):
        super().__init__()
        self.runtime = runtime
        self.settings: Settings = runtime.settings
        self.store = runtime.store
        self.history: list[dict[str, str]] = []
        self._last_sources: list[Source] = []
        self._last_answer_id: int | None = None
        self._answer_sources: dict[int, list[Source]] = {}
        self._answer_serial = 0
        self._assistant: Markdown | None = None
        self._busy = False

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Static(self._masthead_text(), id="masthead")
        with Horizontal(id="body"):
            with Vertical(id="shelf"):
                yield Static("", id="shelf-head")
                with VerticalScroll(id="sources"):
                    yield Markdown(self.SOURCES_HINT, open_links=False, classes="brief")
            with Vertical(id="desk"):
                yield VerticalScroll(id="chat")
        yield Input(
            placeholder="Ask for contract language…  (/help for commands)", id="prompt"
        )
        yield Static("", id="statusbar", markup=False)
        yield Footer()

    def _masthead_text(self) -> Text:
        text = Text()
        text.append("CBA ", style="bold #d9a441")
        text.append("DESK", style="bold #e8ddc8")
        text.append("\u00a0·\u00a0contract language, newest first", style="#9db3a4")
        text.append("\u00a0·\u00a0", style="#3b3a2f")
        text.append("OpenCode Go", style="#7d9d8c")
        return text

    def _shelf_head_text(self) -> Text:
        counts = {"current": 0, "expired": 0, "upcoming": 0, "undated": 0}
        for contract in self.runtime.contracts:
            counts[contract.status] = counts.get(contract.status, 0) + 1
        text = Text()
        text.append("THE SHELF", style="bold #d9a441")
        text.append(
            f"\u00a0·\u00a0corpus {len(self.runtime.contracts)}\n", style="#7d7a6b"
        )
        for position, status in enumerate(("current", "expired", "upcoming", "undated")):
            color, glyph = STATUS_STYLE[status]
            text.append(f"{glyph}{counts.get(status, 0)} {status}", style=color)
            text.append("\n" if position == 1 else "   ")
        return text

    def on_mount(self) -> None:
        self.query_one("#shelf-head", Static).update(self._shelf_head_text())
        self._refresh_status()
        self._system_message(
            "**Ask for clause language across the agreement shelf.** Matching "
            "contracts appear on the left, newest and current first; expired "
            "agreements stay retrievable but are dimmed and down-ranked.\n\n"
            "Try: *Draft a remote-work clause for a public-sector unit* or "
            "*What do current contracts say about arbitration costs?*\n\n"
            "If the shelf is empty, run `cba-bot scrape` then `cba-bot index`."
        )
        self.query_one("#prompt", Input).focus()

    def _chat(self) -> VerticalScroll:
        return self.query_one("#chat", VerticalScroll)

    def _shelf(self) -> VerticalScroll:
        return self.query_one("#sources", VerticalScroll)

    def _system_message(self, text: str) -> None:
        self._chat().mount(Markdown(text, classes="msg brief", open_links=False))
        self._scroll_end()

    def _user_message(self, text: str) -> None:
        self._chat().mount(Static(escape(text), classes="msg user-msg"))
        self._scroll_end()

    def _scroll_end(self) -> None:
        self.call_after_refresh(self._chat().scroll_end, animate=False)

    def _refresh_status(self) -> None:
        try:
            documents = self.store.document_count()
            chunks = self.store.chunk_count()
        except Exception:
            documents = chunks = 0
        filters = "fresh" if self.settings.current_only else "all"
        status = (
            f"{self.runtime.llm.model} · {documents:,} contracts / {chunks:,} "
            f"clauses · terms:{filters} · sector:{self.settings.sector} "
            f"· state:{self.settings.jurisdiction}"
        )
        self.query_one("#statusbar", Static).update(status)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = busy
        if not busy:
            prompt.focus()

    # ------------------------------------------------------------------ shelf
    def _set_sources(self, sources: list[Source], answer_id: int | None) -> None:
        self._last_sources = sources
        self._last_answer_id = answer_id
        if answer_id is not None:
            self._answer_sources[answer_id] = list(sources)
        shelf = self._shelf()
        for child in list(shelf.children):
            child.remove()
        if not sources:
            shelf.mount(Markdown("_No matching contracts._", classes="brief"))
            return
        for source in sources:
            shelf.mount(SourceCard(source, answer_id or 0))
        shelf.scroll_home(animate=False)

    def on_source_card_selected(self, event: SourceCard.Selected) -> None:
        sources = self._answer_sources.get(event.answer_id) or self._last_sources
        source = next((s for s in sources if s.index == event.index), None)
        if source:
            self._open_source(source)

    def _open_source(self, source: Source) -> None:
        found = self.store.fetch_contract(source.contract.id)
        text = found[1] if found else "\n\n".join(c.text for c in source.chunks)
        self.push_screen(ReaderScreen(source, text))

    # ------------------------------------------------------------------ input
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text or self._busy:
            return
        if text.startswith("/"):
            self._handle_command(text)
            return
        self._user_message(text)
        self._set_sources([], None)
        self._answer_serial += 1
        answer_id = self._answer_serial
        self._assistant = Markdown("▌", classes="msg", open_links=False)
        self._chat().mount(self._assistant)
        # Lock the prompt before scheduling the worker: two Submitted events
        # in one tick must not start two answers.
        self._set_busy(True)
        self._answer(text, answer_id)

    def _handle_command(self, text: str) -> None:
        command, _, argument = text[1:].partition(" ")
        command = command.lower().strip()
        argument = argument.strip()

        if command in {"quit", "exit", "q"}:
            self.exit()
        elif command == "help":
            self._system_message(HELP_TEXT)
        elif command == "clear":
            self.action_clear_chat()
        elif command == "sources":
            self._set_sources(self._last_sources, self._last_answer_id)
        elif command == "fresh":
            if argument in {"on", "true", "1", "yes"}:
                self.settings.current_only = True
            elif argument in {"off", "false", "0", "no"}:
                self.settings.current_only = False
            elif not argument:
                self.settings.current_only = not self.settings.current_only
            else:
                self.notify("Use /fresh on|off", severity="warning")
                return
            self._refresh_status()
            self._system_message(
                "**Fresh only** is now "
                + ("**on** — expired agreements are excluded."
                   if self.settings.current_only
                   else "**off** — expired agreements may appear, dimmed.")
            )
        elif command == "sector":
            value = argument.lower() or "all"
            if value not in SECTORS:
                self.notify(f"Use /sector {'|'.join(SECTORS)}", severity="warning")
                return
            self.settings.sector = value
            self._refresh_status()
            self._system_message(f"Sector filter: **{value}**.")
        elif command in {"state", "jurisdiction"}:
            self.settings.jurisdiction = (
                argument.upper() if argument and argument.lower() != "all" else "all"
            )
            self._refresh_status()
            self._system_message(
                f"State filter: **{self.settings.jurisdiction}**."
            )
        elif command == "model":
            if argument:
                self.settings.model = argument
                self.runtime.llm.model = argument
                self._refresh_status()
                self._system_message(f"Chat model set to **{argument}**.")
            else:
                self._show_models()
        elif command == "open":
            try:
                index = int(argument)
            except ValueError:
                self.notify("Use /open <source number>", severity="warning")
                return
            source = next((s for s in self._last_sources if s.index == index), None)
            if source:
                self._open_source(source)
            else:
                self.notify("No such source.", severity="warning")
        elif command == "stats":
            self._show_stats()
        else:
            self.notify(f"Unknown command /{command}", severity="warning")

    @work(exclusive=True)
    async def _show_stats(self) -> None:
        stats = self.store.stats()
        status = ", ".join(f"{k} {v}" for k, v in stats["status"].items())
        sectors = ", ".join(f"{k} {v}" for k, v in stats["sector"].items())
        self._system_message(
            f"**Index**\n\n- {stats['documents']:,} contracts, "
            f"{stats['chunks']:,} clauses, {stats['vectors']:,} vectors "
            f"({stats['vector_dim']}d)\n- status: {status}\n- sector: {sectors}\n"
            f"- data: `{stats['data_dir']}`\n- corpus: "
            f"`{self.settings.contracts_path()}`"
        )

    @work(exclusive=True)
    async def _show_models(self) -> None:
        try:
            models = await list_models(self.settings, api_key=self.runtime.llm.api_key)
        except Exception as exc:
            self.notify(f"Could not list models: {exc}", severity="error", timeout=8)
            self._system_message(
                f"**Could not list models:** {escape(str(exc))}\n\n"
                "Set one with `/model <name>`."
            )
            return
        if not models:
            self._system_message("No models reported. Use `/model <name>`.")
            return
        lines = ["**OpenCode Go models**", ""]
        for name in models:
            marker = " ← current" if name == self.runtime.llm.model else ""
            lines.append(f"- `{name}`{marker}")
        lines += ["", "Switch with `/model <name>`."]
        self._system_message("\n".join(lines))

    def action_clear_chat(self) -> None:
        if self._busy:
            self.notify("Wait for the current answer to finish.", severity="warning")
            return
        chat = self._chat()
        for child in list(chat.children):
            child.remove()
        self.history.clear()
        self._answer_sources.clear()
        self._last_sources = []
        self._last_answer_id = None
        self._answer_serial = 0
        self._assistant = None
        self._set_sources([], None)
        self._system_message("Conversation cleared.")

    # ------------------------------------------------------------- citations
    def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        if self.screen.get_selected_text():
            return
        if event.href.startswith("cba-source:"):
            event.stop()
            try:
                _, answer_id, index = event.href.split(":")
                answer_id_i, index_i = int(answer_id), int(index)
            except ValueError:
                return
            sources = self._answer_sources.get(answer_id_i) or self._last_sources
            source = next((s for s in sources if s.index == index_i), None)
            if source:
                self._open_source(source)
            return
        if event.href.startswith(("http://", "https://")):
            event.stop()
            self.open_url(event.href)

    # ----------------------------------------------------------------- answer
    @work(exclusive=True)
    async def _answer(self, question: str, answer_id: int) -> None:
        self._set_busy(True)
        assistant = self._assistant
        parts: list[str] = []
        sources: list[Source] = []
        answer = ""
        had_error = False
        try:
            async for kind, payload in stream_answer(
                self.runtime,
                question,
                self.history,
                current_only=self.settings.current_only,
                sector=self.settings.sector,
                jurisdiction=self.settings.jurisdiction,
            ):
                if kind == "sources":
                    sources = payload  # type: ignore[assignment]
                    self._set_sources(sources, answer_id)
                elif kind == "token":
                    answer += str(payload)
                    parts.append(str(payload))
                    if len(parts) >= 10 and assistant is not None:
                        assistant.update(
                            linkify_citations(answer + " ▌", answer_id, len(sources))
                        )
                        parts.clear()
        except LLMError as exc:
            had_error = True
            answer += f"\n\n**OpenCode Go error:** {escape(str(exc))}"
            self.notify(str(exc), severity="error", timeout=10)
        except Exception as exc:  # pragma: no cover - defensive
            had_error = True
            answer += f"\n\n**Error:** {escape(str(exc))}"
            self.notify(str(exc), severity="error", timeout=10)
        finally:
            if answer_id == self._answer_serial:
                if assistant is not None:
                    body = linkify_citations(
                        answer or "_No answer._", answer_id, len(sources)
                    )
                    assistant.update(
                        body
                        + "\n\n---\n_Contract research, not legal advice._"
                    )
                if answer.strip() and not had_error:
                    self.history.append({"role": "user", "content": question})
                    self.history.append(
                        {"role": "assistant", "content": answer.strip()}
                    )
                self._set_busy(False)
                self._scroll_end()
