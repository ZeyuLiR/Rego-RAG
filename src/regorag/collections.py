"""Knowledge base collections (rego today, sales later), from config/collections.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Collection:
    name: str
    title: str
    categories: tuple[str, ...]
    template_path: Path

    def template(self) -> str:
        return self.template_path.read_text(encoding="utf-8") if self.template_path.is_file() else ""


def load_collections(config_dir: Path, root: Path) -> dict[str, Collection]:
    data = yaml.safe_load((config_dir / "collections.yaml").read_text(encoding="utf-8")) or {}
    return {
        name: Collection(name=name, title=c.get("title", name),
                         categories=tuple(c.get("categories", [])),
                         template_path=root / c.get("template", f"kb/{name}/_template.md"))
        for name, c in (data.get("collections") or {}).items()
    }
