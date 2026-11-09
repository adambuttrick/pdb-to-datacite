"""The PDB entry that builders read."""

import dataclasses
import functools
import gzip
import pathlib
import re
import types
from collections.abc import Mapping

import gemmi


def read_entry(path: pathlib.Path) -> "PdbEntry":
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8")
    # RCSB header files keep an empty "loop_" where coordinates were removed.
    text = re.sub(r"^loop_\s*\n(?=#|loop_|\Z)", "", text, flags=re.MULTILINE)
    return PdbEntry(gemmi.cif.read_string(text).sole_block())


def clean_text(text: str | None) -> str | None:
    return re.sub(r"\s+", " ", text).strip() if text else text


def as_int(value: str | None, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclasses.dataclass(frozen=True)
class Revision:
    """One Structure model row of _pdbx_audit_revision_history."""

    date: str | None
    major: str | None
    minor: str | None
    groups: tuple[str, ...]
    type: str | None


class PdbEntry:
    """Read-only view of one mmCIF block. Values are stripped strings, or None for empty, '?' and '.'."""

    def __init__(self, block: gemmi.cif.Block) -> None:
        self._block = block
        self._rows: dict[str, tuple[Mapping[str, str | None], ...]] = {}

    def rows(self, category: str) -> tuple[Mapping[str, str | None], ...]:
        if category not in self._rows:
            table = self._block.get_mmcif_category(f"_{category}.", raw=False)
            keys = list(table)
            count = len(table[keys[0]]) if keys else 0
            self._rows[category] = tuple(
                types.MappingProxyType({key: _value(table[key][index]) for key in keys}) for index in range(count)
            )
        return self._rows[category]

    def first(self, category: str, item: str) -> str | None:
        return next((row[item] for row in self.rows(category) if row.get(item)), None)

    @functools.cached_property
    def id(self) -> str:
        return self._block.find_value("_entry.id").strip()

    @functools.cached_property
    def pdb_database_row(self) -> Mapping[str, str | None]:
        """The first _database_2 row for PDB."""
        return next((row for row in self.rows("database_2") if row.get("database_id") == "PDB"), types.MappingProxyType({}))

    @functools.cached_property
    def extended_id(self) -> str:
        return self.pdb_database_row.get("pdbx_database_accession") or f"pdb_0000{self.id.lower()}"

    @functools.cached_property
    def deposited(self) -> str | None:
        return self.first("pdbx_database_status", "recvd_initial_deposition_date")

    @functools.cached_property
    def revisions(self) -> tuple[Revision, ...]:
        model = "Structure model"
        history = sorted(
            (row for row in self.rows("pdbx_audit_revision_history") if row.get("data_content_type") == model),
            key=lambda row: as_int(row.get("ordinal")),
        )
        groups: dict[str | None, list[str]] = {}
        for row in self.rows("pdbx_audit_revision_group"):
            if row.get("data_content_type") == model and row.get("group"):
                groups.setdefault(row["revision_ordinal"], []).append(row["group"])
        kinds = {}
        for row in self.rows("pdbx_audit_revision_details"):
            if row.get("data_content_type") == model and row.get("type"):
                kinds[row["revision_ordinal"]] = row["type"]
        return tuple(
            Revision(
                date=row.get("revision_date"),
                major=row["major_revision"],
                minor=row["minor_revision"],
                groups=tuple(dict.fromkeys(groups.get(row["ordinal"], []))),
                type=kinds.get(row["ordinal"]),
            )
            for row in history
        )


def _value(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
