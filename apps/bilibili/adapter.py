from pathlib import Path

from ..base import AppAdapter, OperationCatalog
from ..catalog import load_catalog


class BilibiliAdapter(AppAdapter):
    def __init__(self, catalog_path: str | Path | None = None) -> None:
        self._catalog = load_catalog(catalog_path or Path(__file__).with_name("catalog.json"))

    @property
    def app_id(self) -> str:
        return self._catalog.app_id

    @property
    def catalog(self) -> OperationCatalog:
        return self._catalog
