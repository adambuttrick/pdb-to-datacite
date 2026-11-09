"""Load the YAML mapping into a Crosswalk, and convert entries with it.

load() checks the whole mapping, so conversion finds no mapping error except a regex that does not compile.
"""

import csv
import dataclasses
import functools
import inspect
import itertools
import json
import pathlib
from collections.abc import Callable, Iterator
from typing import Any

import yaml

from . import builders, datacite
from .entry import PdbEntry, read_entry


class MappingError(ValueError):
    """The YAML or a lookup file is wrong. The message starts with the YAML path, such as `optional.subjects.sources[2]`."""


@dataclasses.dataclass(frozen=True)
class Conversion:
    """missing names the mandatory properties that have no value, in mapping order; it is empty for a complete record."""

    record: dict[str, Any]
    missing: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Property:
    name: str
    mandatory: bool
    many: bool
    distinct: Callable[[Any], str | None] | None
    sources: tuple[Callable[[PdbEntry], Iterator[Any]], ...]


@dataclasses.dataclass(frozen=True)
class Crosswalk:
    """A loaded mapping.

    properties: the mandatory block, then the optional block, in file order.
    input_files: every file whose content changes records: the YAML, the lookup files and this package's .py files.
    """

    properties: tuple[Property, ...]
    input_files: tuple[pathlib.Path, ...]

    def convert(self, path: pathlib.Path) -> Conversion:
        """The DataCite `attributes` object for one mmCIF file.

        A list property gets the values of all its sources. A single-value
        property gets the first value, so a later source runs only when the
        earlier ones give nothing.
        """
        entry = read_entry(path)
        record = {}
        missing = []
        for prop in self.properties:
            values = itertools.chain.from_iterable(source(entry) for source in prop.sources)
            if prop.distinct:
                values = _distinct(values, prop.distinct)
            value = list(values) if prop.many else next(values, None)
            if value is None or value == []:
                if prop.mandatory:
                    missing.append(prop.name)
                continue
            record[prop.name] = value
        return Conversion(record, tuple(missing))


def load(path: pathlib.Path, lookups: dict[str, pathlib.Path]) -> Crosswalk:
    """Read and check the mapping at path. lookups gives the file for each lookup the mapping declares."""
    document = _read_yaml(path)
    for key in document:
        if key not in ("lookups", "definitions", "mandatory", "optional"):
            raise MappingError(f"{key}: unknown top-level key; expected lookups, definitions, mandatory or optional")
    declared = _mapping(document.get("lookups"), "lookups")
    tables = _load_lookups(declared, lookups)
    catalog = datacite.catalog()
    registry = _builders()
    properties = []
    tiers = {}
    for tier in ("mandatory", "optional"):
        for name, spec in _mapping(document.get(tier), tier).items():
            where = f"{tier}.{name}"
            if name not in catalog:
                raise MappingError(f"{where}: not a DataCite property; expected one of {', '.join(catalog)}")
            if name in tiers:
                raise MappingError(f"{where}: also in the {tiers[name]} block")
            tiers[name] = tier
            properties.append(_property(name, tier == "mandatory", catalog[name].many, spec, where, registry, tables))
    for name, spec in catalog.items():
        if spec.mandatory and tiers.get(name) != "mandatory":
            raise MappingError(f"{tiers.get(name, 'mandatory')}.{name}: DataCite makes {name} mandatory, "
                               "so it must be in the mandatory block")
    package = sorted(pathlib.Path(__file__).parent.glob("*.py"))
    return Crosswalk(tuple(properties), (path, *(lookups[name] for name in sorted(declared)), *package))


def _property(
    name: str, mandatory: bool, many: bool, spec: Any, where: str,
    registry: dict[str, Callable[..., Iterator[Any]]], tables: dict[str, dict[str, dict[str, str]]],
) -> Property:
    spec = _mapping(spec, where)
    for key in spec:
        if key not in ("sources", "distinct"):
            raise MappingError(f"{where}.{key}: unknown key; expected sources or distinct")
    sources = spec.get("sources")
    if not isinstance(sources, list) or not sources:
        raise MappingError(f"{where}.sources: expected a non-empty list of sources")
    distinct_keys = {"value": _whole_value, "unschemed_subject": _unschemed_subject}
    distinct = spec.get("distinct")
    if distinct is not None and (not isinstance(distinct, str) or distinct not in distinct_keys):
        raise MappingError(f"{where}.distinct: unknown {distinct!r}; expected one of {', '.join(distinct_keys)}")
    bound = tuple(_bind(source, f"{where}.sources[{index}]", registry, tables) for index, source in enumerate(sources))
    return Property(name, mandatory, many, distinct_keys.get(distinct), bound)


def _bind(
    source: Any, where: str,
    registry: dict[str, Callable[..., Iterator[Any]]], tables: dict[str, dict[str, dict[str, str]]],
) -> Callable[[PdbEntry], Iterator[Any]]:
    if not isinstance(source, dict) or len(source) != 1:
        raise MappingError(f"{where}: expected one builder name with its parameters")
    [(name, params)] = source.items()
    if name not in registry:
        raise MappingError(f"{where}: unknown builder {name!r}; expected one of {', '.join(sorted(registry))}")
    where = f"{where}.{name}"
    params = _with_lookups(_mapping(params, where), where, tables)
    signature = inspect.signature(registry[name])
    try:
        # bind_partial first, so a misspelt parameter is named as unknown, not reported as a missing one.
        signature.bind_partial(None, **params)
        signature.bind(None, **params)
    except TypeError as error:
        raise MappingError(f"{where}: {error}") from None
    return functools.partial(registry[name], **params)


def _builders() -> dict[str, Callable[..., Iterator[Any]]]:
    """The public functions of builders.py, by name."""
    return {
        name: function
        for name, function in inspect.getmembers(builders, inspect.isfunction)
        if function.__module__ == builders.__name__ and not name.startswith("_")
    }


def _with_lookups(value: Any, where: str, tables: dict[str, dict[str, dict[str, str]]]) -> Any:
    """A copy of value with each `{lookup: NAME}` replaced by that lookup's rows, by key."""
    match value:
        case {"lookup": str(name)} if len(value) == 1:
            if name not in tables:
                raise MappingError(f"{where}: lookup {name!r} is not declared under lookups")
            return tables[name]
        case dict():
            return {key: _with_lookups(part, f"{where}.{key}", tables) for key, part in value.items()}
        case list():
            return [_with_lookups(part, f"{where}[{index}]", tables) for index, part in enumerate(value)]
        case _:
            return value


def _load_lookups(declared: dict[str, Any], paths: dict[str, pathlib.Path]) -> dict[str, dict[str, dict[str, str]]]:
    for name in paths:
        if name not in declared:
            raise MappingError(f"lookups.{name}: a file is given for {name!r}, but the mapping does not declare it")
    tables = {}
    for name, spec in declared.items():
        where = f"lookups.{name}"
        if not isinstance(spec, dict) or set(spec) != {"key_column"}:
            raise MappingError(f"{where}: expected only key_column")
        if name not in paths:
            raise MappingError(f"{where}: no file is given for this lookup")
        tables[name] = _read_lookup(paths[name], spec["key_column"], where)
    return tables


def _read_lookup(path: pathlib.Path, key: str, where: str) -> dict[str, dict[str, str]]:
    """CSV rows by the key column. On a repeated key the last row wins."""
    try:
        with path.open(encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if key not in (reader.fieldnames or ()):
                raise MappingError(f"{where}.key_column: {path} has no column {key!r}")
            return {row[key]: row for row in reader}
    except OSError as error:
        raise MappingError(f"{where}: {error}") from None


def _read_yaml(path: pathlib.Path) -> dict[str, Any]:
    """A repeated key is an error, because PyYAML silently keeps the last one."""
    try:
        loader = yaml.SafeLoader(path.read_text(encoding="utf-8"))
        try:
            node = loader.get_single_node()
            _check_unique_keys(node, "")
            document = loader.construct_document(node) if node else None
        finally:
            loader.dispose()
    except (OSError, yaml.YAMLError) as error:
        raise MappingError(str(error)) from None
    return _mapping(document, "top level")


def _check_unique_keys(node: yaml.Node, where: str) -> None:
    if isinstance(node, yaml.MappingNode):
        seen = set()
        for key_node, value_node in node.value:
            path = f"{where}.{key_node.value}" if where else str(key_node.value)
            if key_node.value in seen:
                raise MappingError(f"{path}: duplicate key (line {key_node.start_mark.line + 1})")
            seen.add(key_node.value)
            _check_unique_keys(value_node, path)
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            _check_unique_keys(item, f"{where}[{index}]")


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MappingError(f"{where}: expected a mapping")
    return value


def _distinct(values: Iterator[Any], key: Callable[[Any], str | None]) -> Iterator[Any]:
    """The first value with each key. A value whose key is None is always kept."""
    seen = set()
    for value in values:
        value_key = key(value)
        if value_key is None or value_key not in seen:
            seen.add(value_key)
            yield value


def _whole_value(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _unschemed_subject(subject: dict[str, str]) -> str | None:
    """Keywords and molecule names have no scheme, so one case-insensitive check removes repeats across the two."""
    return None if "subjectScheme" in subject else subject["subject"].casefold()
