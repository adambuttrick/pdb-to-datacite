"""Source builders. Each public function is a builder, by its name in the mapping YAML.

A builder takes the PdbEntry and keyword parameters from the YAML, and yields
zero or more DataCite values in record order, never None or an empty value.
Helpers start with an underscore.

A resolver is a YAML mapping that turns a PDB value into an identifier:
pattern (must match the whole value), and optional strip (a regex removed
first), reject (values to refuse), lower (lowercase the named groups),
template (filled from the named groups; without it the value is the match),
scheme and scheme_uri (for the builders that write them).
"""

import copy
import re
from collections.abc import Iterator, Mapping
from typing import Any

from .entry import PdbEntry, as_int, clean_text


def constant(entry: PdbEntry, *, values: list[Any]) -> Iterator[Any]:
    yield from copy.deepcopy(values)


def flag_constant(entry: PdbEntry, *, category: str, item: str, equals: str, values: list[Any]) -> Iterator[Any]:
    if entry.first(category, item) == equals:
        yield from copy.deepcopy(values)


def field(
    entry: PdbEntry,
    *,
    item: str | list[str],
    emit: Any,
    where: dict[str, str] | None = None,
    order_by: str | None = None,
    split: str | None = None,
    clean: bool = True,
    case: str | None = None,
    resolver: dict[str, Any] | None = None,
    distinct: bool = False,
    first: bool = False,
) -> Iterator[Any]:
    """One value per row of one or more "category.item" names, in the order given.

    Steps per value: split at `split` and strip each part; collapse whitespace
    unless clean is false; change case (upper or lower); resolve. An empty
    value is skipped, a repeat too when distinct. Each value fills "{value}" in
    every string of emit. first: only the first value.
    """
    seen = set()
    for name in [item] if isinstance(item, str) else item:
        category, item_name = name.split(".")
        rows = [row for row in entry.rows(category) if all(row.get(key) == value for key, value in (where or {}).items())]
        if order_by:
            rows.sort(key=lambda row: as_int(row.get(order_by)))
        for row in rows:
            raw = row.get(item_name)
            for value in [raw] if split is None else [part.strip() for part in (raw or "").split(split)]:
                if clean:
                    value = clean_text(value)
                if value and case:
                    value = {"upper": str.upper, "lower": str.lower}[case](value)
                if value and resolver:
                    value = _resolve(resolver, value)
                if not value or (distinct and value in seen):
                    continue
                seen.add(value)
                yield _fill(emit, value)
                if first:
                    return


def pdb_database_value(
    entry: PdbEntry, *, item: str, resolver: dict[str, Any], entry_resolver: dict[str, Any] | None = None
) -> Iterator[str]:
    """The resolved item of the first _database_2 row for PDB.

    With entry_resolver, the value must also name this entry: it is the value that
    entry_resolver gives for the entry ID, or it contains the extended PDB ID (letter
    case ignored). For example, the file of 7QNP gives the DOI 10.2210/pdb7np/pdb.
    """
    value = _resolve(resolver, entry.pdb_database_row.get(item))
    if value and (entry_resolver is None or _names_entry(entry, value, entry_resolver)):
        yield value


def entry_id_value(entry: PdbEntry, *, resolver: dict[str, Any]) -> Iterator[str]:
    if value := _resolve(resolver, entry.id):
        yield value


def entry_url(entry: PdbEntry, *, template: str) -> Iterator[str]:
    yield template.format(extended_id=entry.extended_id.lower())


def persons(
    entry: PdbEntry, *, category: str, order_by: str, name_item: str, orcid_item: str, orcid: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    for row in _sorted_rows(entry, category, order_by):
        if row.get(name_item):
            yield _person(row[name_item], row.get(orcid_item), orcid)


def contact_persons(
    entry: PdbEntry,
    *,
    category: str,
    order_by: str,
    family_item: str,
    given_items: list[str],
    orcid_item: str,
    orcid: dict[str, Any],
    contributor_type: str,
) -> Iterator[dict[str, Any]]:
    for row in _sorted_rows(entry, category, order_by):
        family = row.get(family_item)
        if not family:
            continue
        given = " ".join(part for part in (row.get(name) for name in given_items) if part)
        # The family name has its own item, so a comma in it ("Herzik, Jr.") does not split it.
        person: dict[str, Any] = {"name": f"{family}, {given}" if given else family, "nameType": "Personal", "familyName": family}
        if given:
            person["givenName"] = given
        person.update(_name_identifiers(row.get(orcid_item), orcid))
        person["contributorType"] = contributor_type
        yield person


def coded_organization(
    entry: PdbEntry, *, category: str, item: str, names: dict[str, str], fixed: dict[str, str]
) -> Iterator[dict[str, str]]:
    if code := entry.first(category, item):
        yield {"name": names.get(code.upper(), code), **fixed}


def keyword_subjects(
    entry: PdbEntry, *, category: str, whole_item: str, split_item: str, separator: str
) -> Iterator[dict[str, str]]:
    """Per row, the whole keyword item and then each part of the split item."""
    for row in entry.rows(category):
        for text in (row.get(whole_item), *(row.get(split_item) or "").split(separator)):
            if text := clean_text(text):
                yield {"subject": text}


def taxonomy_subjects(
    entry: PdbEntry, *, sources: list[dict[str, str]], separator: str, scheme: str, scheme_uri: str, value_uri: str
) -> Iterator[dict[str, str]]:
    """The first name for each taxonomy ID, across the sources in order.

    Names pair with IDs by position. When the counts differ, one ID takes the
    whole name item and more IDs take no name.
    """
    names: dict[str, str] = {}
    for source in sources:
        for row in entry.rows(source["category"]):
            whole = row.get(source["name_item"])
            ids = _parts(row.get(source["id_item"]), separator)
            parts = _parts(whole, separator)
            if len(parts) != len(ids):
                parts = [whole] * len(ids) if len(ids) == 1 else [None] * len(ids)
            for code, name in zip(ids, parts):
                if code.isdigit() and name and code not in names:
                    names[code] = name
    for code, name in names.items():
        yield {"subject": name, "subjectScheme": scheme, "schemeUri": scheme_uri,
               "valueUri": value_uri.format(code=code), "classificationCode": code}


def release_year(entry: PdbEntry) -> Iterator[str]:
    """Only the first revision counts."""
    if entry.revisions and entry.revisions[0].date:
        yield entry.revisions[0].date[:4]


def deposition_year(entry: PdbEntry) -> Iterator[str]:
    if entry.deposited:
        yield entry.deposited[:4]


def deposition_date(entry: PdbEntry, *, date_type: str) -> Iterator[dict[str, str]]:
    if entry.deposited:
        yield {"date": entry.deposited, "dateType": date_type}


def revision_dates(
    entry: PdbEntry, *, first: str, later: str, obsolete_revision_type: str, obsolete: str
) -> Iterator[dict[str, str]]:
    """One date per dated revision. Only the first revision gets the `first` date type."""
    for index, revision in enumerate(entry.revisions):
        if not revision.date:
            continue
        date_type = first if index == 0 else obsolete if revision.type == obsolete_revision_type else later
        value = {"date": revision.date, "dateType": date_type}
        if info := ", ".join(revision.groups) or revision.type:
            value["dateInformation"] = info
        yield value


def pdb_ids(entry: PdbEntry, *, id_type: str, extended_type: str) -> Iterator[dict[str, str]]:
    yield {"alternateIdentifier": entry.id, "alternateIdentifierType": id_type}
    if entry.extended_id.lower() != entry.id.lower():
        yield {"alternateIdentifier": entry.extended_id, "alternateIdentifierType": extended_type}


def citation_identifiers(entry: PdbEntry, *, citations: dict[str, Any], article_type: str) -> Iterator[dict[str, str]]:
    for row in entry.rows(citations["category"]):
        yield from _citation_identifiers(row, citations, article_type)


def citation_items(
    entry: PdbEntry,
    *,
    citations: dict[str, Any],
    title_item: str,
    book_item: str,
    article_type: str,
    book_type: str,
    authors: dict[str, str],
    fields: dict[str, str],
) -> Iterator[dict[str, Any]]:
    """The full reference of each published citation that gave no related identifier."""
    author_rows = entry.rows(authors["category"])
    for row in entry.rows(citations["category"]):
        if _citation_identifiers(row, citations, None) or not row.get(title_item) or not _published(row, citations):
            continue
        item = {"relationType": _citation_relation(row, citations),
                "relatedItemType": book_type if row.get(book_item) else article_type}
        names = sorted(
            (author for author in author_rows
             if author.get(authors["citation_item"]) == row.get(citations["id_item"]) and author.get(authors["name_item"])),
            key=lambda author: as_int(author.get(authors["order_by"])),
        )
        if names:
            item["creators"] = [_person(author[authors["name_item"]], None, None) for author in names]
        item["titles"] = [{"title": clean_text(row[title_item])}]
        for source, target in fields.items():
            if row.get(source):
                item[target] = row[source]
        yield item


def related_entries(
    entry: PdbEntry,
    *,
    category: str,
    db_item: str,
    id_item: str,
    label_item: str,
    skip_labels: list[str],
    relation: str,
    resource_type: str,
    resolvers: dict[str, dict[str, Any]],
) -> Iterator[dict[str, str]]:
    """Links to entries in other databases, each database with its own resolver."""
    for row in entry.rows(category):
        label = row.get(label_item)
        resolver = resolvers.get(row.get(db_item))
        identifier = _resolve(resolver, row.get(id_item)) if resolver else None
        if not identifier or not label or label in skip_labels:
            continue
        yield {"relatedIdentifier": identifier, "relatedIdentifierType": resolver["scheme"], "relationType": relation,
               "relationTypeInformation": label, "resourceTypeGeneral": resource_type}


def supersessions(
    entry: PdbEntry,
    *,
    category: str,
    kind_item: str,
    new_item: str,
    old_item: str,
    replaces: dict[str, str],
    replaced_by: dict[str, str],
    resolver: dict[str, Any],
    resource_type: str,
) -> Iterator[dict[str, str]]:
    """A row of kind `replaces` relates this entry to the entries it replaced; `replaced_by` to its successors.

    Both ID items can hold more than one ID, with spaces between them. For example, 2HHB, 3HHB
    and 4HHB replace 1HHB. A value that is not a PDB ID, such as NONE, gives no relation.
    """
    own = entry.id.upper()
    for row in entry.rows(category):
        news = (row.get(new_item) or "").upper().split()
        olds = (row.get(old_item) or "").upper().split()
        if row.get(kind_item) == replaces["kind"] and own in news:
            targets = [(old, replaces["relation"]) for old in olds]
        elif row.get(kind_item) == replaced_by["kind"] and own in olds:
            targets = [(new, replaced_by["relation"]) for new in news]
        else:
            continue
        for code, relation in targets:
            if identifier := _resolve(resolver, code):
                yield {"relatedIdentifier": identifier, "relatedIdentifierType": resolver["scheme"],
                       "relationType": relation, "resourceTypeGeneral": resource_type}


def deposit_groups(
    entry: PdbEntry, *, category: str, id_item: str, title_item: str, relation: str, item_type: str, number_type: str
) -> Iterator[dict[str, Any]]:
    for row in entry.rows(category):
        if group := row.get(id_item):
            yield {"relationType": relation, "relatedItemType": item_type,
                   "titles": [{"title": clean_text(row.get(title_item)) or group}],
                   "number": group, "numberType": number_type}


def software_items(
    entry: PdbEntry, *, categories: list[str], name_item: str, version_item: str, relation: str, item_type: str
) -> Iterator[dict[str, Any]]:
    """One item per distinct (name ignoring case, version) across the categories in order."""
    seen = set()
    for category in categories:
        for row in entry.rows(category):
            name, version = clean_text(row.get(name_item)), clean_text(row.get(version_item))
            if not name or (name.casefold(), version) in seen:
                continue
            seen.add((name.casefold(), version))
            item = {"relationType": relation, "relatedItemType": item_type, "titles": [{"title": name}]}
            if version:
                item["edition"] = version
            yield item


def last_revision_version(entry: PdbEntry) -> Iterator[str]:
    if entry.revisions:
        yield f"{entry.revisions[-1].major}.{entry.revisions[-1].minor}"


def funding(
    entry: PdbEntry,
    *,
    category: str,
    order_by: str,
    name_item: str,
    award_item: str,
    funders: dict[str, dict[str, str]],
    id_column: str,
    status_column: str,
    skip_statuses: list[str],
    identifier_type: str,
    scheme_uri: str,
) -> Iterator[dict[str, str]]:
    for row in _sorted_rows(entry, category, order_by):
        name = clean_text(row.get(name_item))
        funder = funders.get(name or "", {})
        if not name or funder.get(status_column) in skip_statuses:
            continue
        value = {"funderName": name}
        if funder.get(id_column):
            value.update({"funderIdentifier": funder[id_column], "funderIdentifierType": identifier_type,
                          "schemeUri": scheme_uri})
        if row.get(award_item):
            value["awardNumber"] = row[award_item]
        yield value


def _resolve(resolver: dict[str, Any], value: str | None) -> str | None:
    value = (value or "").strip()
    if "strip" in resolver:
        value = re.sub(resolver["strip"], "", value)
    match = re.fullmatch(resolver["pattern"], value)
    if not match or value in resolver.get("reject", ()):
        return None
    if "template" not in resolver:
        return match[0]
    groups = {name: group.lower() if resolver.get("lower") else group for name, group in match.groupdict().items()}
    return resolver["template"].format(**groups)


def _fill(template: Any, value: str) -> Any:
    match template:
        case str():
            return template.format(value=value)
        case dict():
            return {key: _fill(part, value) for key, part in template.items()}
        case list():
            return [_fill(part, value) for part in template]
        case _:
            return template


def _person(name: str, orcid_value: str | None, orcid: dict[str, Any] | None) -> dict[str, Any]:
    """Splits "Family, Given" at the first comma."""
    person: dict[str, Any] = {"name": name}
    if "," in name:
        family, given = (part.strip() for part in name.split(",", 1))
        person.update({"nameType": "Personal", "familyName": family})
        if given:
            person["givenName"] = given
    person.update(_name_identifiers(orcid_value, orcid))
    return person


def _name_identifiers(orcid_value: str | None, orcid: dict[str, Any] | None) -> dict[str, Any]:
    if orcid and (identifier := _resolve(orcid, orcid_value)):
        return {"nameIdentifiers": [{"nameIdentifier": identifier, "nameIdentifierScheme": orcid["scheme"],
                                     "schemeUri": orcid["scheme_uri"]}]}
    return {}


def _names_entry(entry: PdbEntry, value: str, entry_resolver: dict[str, Any]) -> bool:
    own = _resolve(entry_resolver, entry.id)
    return (own is not None and value.lower() == own.lower()) or entry.extended_id.lower() in value.lower()


def _citation_relation(row: Mapping[str, str | None], citations: dict[str, Any]) -> str:
    return citations["relations"].get(row.get(citations["id_item"]), citations["other_relation"])


def _published(row: Mapping[str, str | None], citations: dict[str, Any]) -> bool:
    journal = row.get(citations["journal_item"]) or ""
    return bool(journal) and journal.casefold() != citations["unpublished_journal"].casefold()


def _citation_identifiers(row: Mapping[str, str | None], citations: dict[str, Any], article_type: str | None) -> list[dict[str, str]]:
    relation = _citation_relation(row, citations)
    found = []
    for item, resolver in citations["identifiers"].items():
        if identifier := _resolve(resolver, row.get(item)):
            found.append({"relatedIdentifier": identifier, "relatedIdentifierType": resolver["scheme"],
                          "relationType": relation})
    if article_type and _published(row, citations):
        for identifier in found:
            identifier["resourceTypeGeneral"] = article_type
    return found


def _sorted_rows(entry: PdbEntry, category: str, order_by: str) -> list[Mapping[str, str | None]]:
    return sorted(entry.rows(category), key=lambda row: as_int(row.get(order_by)))


def _parts(value: str | None, separator: str) -> list[str]:
    return [part.strip() for part in (value or "").split(separator) if part.strip()]
