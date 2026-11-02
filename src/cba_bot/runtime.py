"""Shared runtime: settings, corpus catalog, index, embedder, LLM."""

from __future__ import annotations

from dataclasses import dataclass

from cba_bot.catalog import Contract, discover_contracts
from cba_bot.config import Settings, resolve_api_key
from cba_bot.index import Embedder, Store
from cba_bot.llm import LLMClient


@dataclass
class Runtime:
    settings: Settings
    store: Store
    embedder: Embedder
    llm: LLMClient
    contracts: list[Contract]

    @classmethod
    def build(cls, settings: Settings, *, api_key: str | None = None) -> Runtime:
        data = settings.data_path()
        store = Store(data / "index")
        contracts = discover_contracts(settings.contracts_path())
        key = resolve_api_key(settings, api_key) if settings.provider != "mock" else None
        return cls(
            settings=settings,
            store=store,
            embedder=Embedder(settings.embed_model),
            llm=LLMClient(settings, api_key=key),
            contracts=contracts,
        )

    def close(self) -> None:
        self.store.close()
