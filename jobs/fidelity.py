# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "huggingface_hub>=2.2,<3",
#     "lxml==6.0.2",
#     "mmcif==1.2.1",
# ]
# ///
"""Fidelity checks for the published DataCite records of the PDB archive.

The checks do not use the converter. They read the published records, and they read each
wwPDB entry file with the wwPDB `mmcif` package (the converter uses gemmi). The expected
record of an entry comes from the rules in CROSSWALK.md, written again in this script.

Record checks (all records, no entry files):
  coverage    each holdings entry has one record or one incomplete record, and no other record exists
  manifest    MANIFEST.json agrees with the shards
  pairing     the JSON and XML shards give the same DOIs in the same order
  xml         the XML record gives the same values as the JSON record
  xsd         the XML record is valid for the DataCite 4.7 XSD (libxml2)
  mandatory   a published record has all mandatory properties; an incomplete record does not
  text        no empty value, mmCIF null marker, replacement character, control character, line break or edge space
  note        two spaces in a value, or an entry file with non-ASCII text (not a failure)

Source checks (each entry file against its record):
  property    each property is equal to the value that the CROSSWALK.md rules give for the entry file
  doi         the record DOI is 10.2210/pdbXXXX/pdb for the entry ID
  accounting  each source value goes into the record or is left out by a named rule

Entry files come from a local copy of the archive (--mirror) or from the bucket mirror (--bucket, --prefix).
"""

import argparse
import collections
import concurrent.futures
import csv
import dataclasses
import datetime
import functools
import gzip
import hashlib
import itertools
import json
import logging
import multiprocessing
import pathlib
import re
import sys
import tempfile


def log(message):
    logging.getLogger("fidelity").info(message)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    records = parser.add_mutually_exclusive_group(required=True)
    records.add_argument("--records", type=pathlib.Path, help="local copy of the published dataset (MANIFEST.json, json/, xml/, incomplete/)")
    records.add_argument("--dataset", help="Hugging Face dataset of the records, REPO@REVISION")
    parser.add_argument("--holdings", type=pathlib.Path, help="directory with the two wwPDB holdings files (default: from the bucket)")
    parser.add_argument("--mirror", type=pathlib.Path, help="local archive root with data/structures/... (default: the bucket mirror)")
    parser.add_argument("--bucket", help="bucket ID, NAMESPACE/BUCKET, for the holdings and the mirror")
    parser.add_argument("--prefix", help="key prefix of the mirror in the bucket, for example pdb")
    parser.add_argument("--funders", required=True, type=pathlib.Path, help="the funder table that the conversion used")
    parser.add_argument("--xsd", required=True, type=pathlib.Path, help="DataCite 4.7 metadata.xsd")
    parser.add_argument("--entries", type=pathlib.Path, help="file with one PDB ID on each line: run the source checks for these entries only")
    parser.add_argument("--skip-source", action="store_true", help="run the record checks only")
    parser.add_argument("--work-dir", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path, help="directory for report.json and findings.jsonl.gz")
    parser.add_argument("--publish", help="also copy the output to this bucket path, NAMESPACE/BUCKET/PATH/")
    parser.add_argument("--workers", required=True, type=int)
    parser.add_argument("--chunk", type=int, default=2000, help="entry files to download from the bucket at a time")
    parser.add_argument("--verbose", action="store_true", help="write progress to stderr")
    args = parser.parse_args()
    if (args.holdings is None or (args.mirror is None and not args.skip_source)) and not (args.bucket and args.prefix):
        parser.error("--bucket and --prefix are necessary without --holdings and --mirror")
    return args


# ---------------------------------------------------------------- inputs


def holdings_names():
    return {"current": "released_structures_last_modified_dates.json.gz", "obsolete": "obsolete_structures_last_modified_dates.json.gz"}


def archive_path(pdb_id, status):
    directory = {"current": "divided", "obsolete": "obsolete"}[status]
    return f"data/structures/{directory}/mmCIF/{pdb_id[1:3]}/{pdb_id}.cif.gz"


def read_holdings(directory):
    """{pdb_id: (status, last_modified)} from the two holdings files."""
    entries = {}
    for status, name in holdings_names().items():
        for pdb_id, stamp in json.loads(gzip.decompress((directory / name).read_bytes())).items():
            if pdb_id.lower() in entries:
                raise SystemExit(f"{pdb_id} is in two holdings files")
            entries[pdb_id.lower()] = (status, stamp)
    return entries


def fetch_holdings(api, bucket, prefix, directory):
    listing = {item.path: item for item in api.list_bucket_tree(bucket, prefix=f"{prefix}/holdings/", recursive=True) if item.type == "file"}
    directory.mkdir(parents=True, exist_ok=True)
    api.download_bucket_files(bucket, [(listing[f"{prefix}/holdings/{name}"], str(directory / name)) for name in holdings_names().values()])
    return directory


def fetch_dataset(spec, directory):
    from huggingface_hub import snapshot_download
    repo, _, revision = spec.partition("@")
    if not revision:
        raise SystemExit("--dataset must be REPO@REVISION")
    path = snapshot_download(repo, repo_type="dataset", revision=revision, local_dir=directory,
                             allow_patterns=["MANIFEST.json", "json/*", "xml/*", "incomplete/*"])
    return pathlib.Path(path)


def read_lines(path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return handle.read().splitlines()


# ---------------------------------------------------------------- record checks


@dataclasses.dataclass
class Findings:
    counts: collections.Counter = dataclasses.field(default_factory=collections.Counter)
    lines: list = dataclasses.field(default_factory=list)

    def add(self, check, pdb_id, **detail):
        self.counts[check] += 1
        self.lines.append({"check": check, "pdb_id": pdb_id, **detail})

    def merge(self, other):
        self.counts.update(other.counts)
        self.lines += other.lines


def pdb_id_of(attributes):
    for identifier in attributes.get("alternateIdentifiers", []):
        if identifier.get("alternateIdentifierType") == "PDB ID":
            return identifier["alternateIdentifier"].lower()
    match = re.fullmatch(r"10\.2210/pdb([0-9a-z]{4})/pdb", attributes.get("doi", "").lower())
    return match[1] if match else None


def xml_to_attributes(xml_text):
    """The JSON attributes that a DataCite 4.7 XML record gives, read with lxml (the converter writes with ElementTree)."""
    from lxml import etree
    ns = "{http://datacite.org/schema/kernel-4}"
    root = etree.fromstring(xml_text.encode("utf-8"))

    def children(element, tag):
        return [] if element is None else element.findall(ns + tag)

    def one(element, tag):
        return None if element is None else element.find(ns + tag)

    def put(target, key, value):
        if value is not None:
            target[key] = value

    def name_entry(element, name_tag):
        entry = {}
        name = one(element, name_tag)
        put(entry, "name", name.text)
        put(entry, "nameType", name.get("nameType"))
        for key in ("givenName", "familyName"):
            node = one(element, key)
            put(entry, key, None if node is None else node.text)
        identifiers = [{"nameIdentifier": node.text, "nameIdentifierScheme": node.get("nameIdentifierScheme"),
                        **({"schemeUri": node.get("schemeURI")} if node.get("schemeURI") else {})}
                       for node in children(element, "nameIdentifier")]
        if identifiers:
            entry["nameIdentifiers"] = identifiers
        return entry

    def attributes_of(node, mapping):
        return {target: node.get(source) for source, target in mapping.items() if node.get(source) is not None}

    record = {}
    identifier = one(root, "identifier")
    put(record, "doi", None if identifier is None else identifier.text)
    if (node := one(root, "creators")) is not None:
        record["creators"] = [name_entry(creator, "creatorName") for creator in children(node, "creator")]
    if (node := one(root, "titles")) is not None:
        record["titles"] = [{"title": title.text, **attributes_of(title, {"titleType": "titleType"})} for title in children(node, "title")]
    if (node := one(root, "publisher")) is not None:
        record["publisher"] = {"name": node.text, **attributes_of(node, {
            "publisherIdentifier": "publisherIdentifier", "publisherIdentifierScheme": "publisherIdentifierScheme", "schemeURI": "schemeUri"})}
    if (node := one(root, "publicationYear")) is not None:
        record["publicationYear"] = node.text
    if (node := one(root, "resourceType")) is not None:
        record["types"] = {"resourceTypeGeneral": node.get("resourceTypeGeneral"), "resourceType": node.text}
    if (node := one(root, "subjects")) is not None:
        record["subjects"] = [{"subject": subject.text, **attributes_of(subject, {
            "subjectScheme": "subjectScheme", "schemeURI": "schemeUri", "valueURI": "valueUri", "classificationCode": "classificationCode"})}
            for subject in children(node, "subject")]
    if (node := one(root, "contributors")) is not None:
        record["contributors"] = [name_entry(contributor, "contributorName") | {"contributorType": contributor.get("contributorType")}
                                  for contributor in children(node, "contributor")]
    if (node := one(root, "dates")) is not None:
        record["dates"] = [{"date": date.text, **attributes_of(date, {"dateType": "dateType", "dateInformation": "dateInformation"})}
                           for date in children(node, "date")]
    if (node := one(root, "language")) is not None:
        record["language"] = node.text
    if (node := one(root, "alternateIdentifiers")) is not None:
        record["alternateIdentifiers"] = [{"alternateIdentifier": item.text, **attributes_of(item, {"alternateIdentifierType": "alternateIdentifierType"})}
                                          for item in children(node, "alternateIdentifier")]
    if (node := one(root, "relatedIdentifiers")) is not None:
        record["relatedIdentifiers"] = [{"relatedIdentifier": item.text, **attributes_of(item, {
            "relatedIdentifierType": "relatedIdentifierType", "relationType": "relationType",
            "relationTypeInformation": "relationTypeInformation", "resourceTypeGeneral": "resourceTypeGeneral"})}
            for item in children(node, "relatedIdentifier")]
    if (node := one(root, "formats")) is not None:
        record["formats"] = [item.text for item in children(node, "format")]
    if (node := one(root, "version")) is not None:
        record["version"] = node.text
    if (node := one(root, "rightsList")) is not None:
        record["rightsList"] = [{"rights": item.text, **attributes_of(item, {
            "rightsURI": "rightsUri", "rightsIdentifier": "rightsIdentifier", "rightsIdentifierScheme": "rightsIdentifierScheme", "schemeURI": "schemeUri"})}
            for item in children(node, "rights")]
    if (node := one(root, "descriptions")) is not None:
        record["descriptions"] = [{"description": item.text, **attributes_of(item, {"descriptionType": "descriptionType"})}
                                  for item in children(node, "description")]
    if (node := one(root, "fundingReferences")) is not None:
        references = []
        for item in children(node, "fundingReference"):
            reference = {"funderName": one(item, "funderName").text}
            if (funder := one(item, "funderIdentifier")) is not None:
                reference |= {"funderIdentifier": funder.text, **attributes_of(funder, {"funderIdentifierType": "funderIdentifierType", "schemeURI": "schemeUri"})}
            if (award := one(item, "awardNumber")) is not None:
                reference["awardNumber"] = award.text
            references.append(reference)
        record["fundingReferences"] = references
    if (node := one(root, "relatedItems")) is not None:
        items = []
        for item in children(node, "relatedItem"):
            value = {"relationType": item.get("relationType"), "relatedItemType": item.get("relatedItemType")}
            if (creators := one(item, "creators")) is not None:
                value["creators"] = [name_entry(creator, "creatorName") for creator in children(creators, "creator")]
            value["titles"] = [{"title": title.text} for title in children(one(item, "titles"), "title")]
            for key in ("publicationYear", "volume", "issue", "firstPage", "lastPage", "edition"):
                if (field := one(item, key)) is not None:
                    value[key] = field.text
            if (number := one(item, "number")) is not None:
                value |= {"number": number.text, **attributes_of(number, {"numberType": "numberType"})}
            items.append(value)
        record["relatedItems"] = items
    known = {"identifier", "creators", "titles", "publisher", "publicationYear", "resourceType", "subjects", "contributors", "dates",
             "language", "alternateIdentifiers", "relatedIdentifiers", "formats", "version", "rightsList", "descriptions",
             "fundingReferences", "relatedItems"}
    unread = sorted({etree.QName(child).localname for child in root} - known)
    return record, unread


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def text_problems(value, path=""):
    """(path, problem) for each string in a JSON value that a conversion must not give."""
    if isinstance(value, dict):
        for key, part in value.items():
            yield from text_problems(part, f"{path}.{key}")
    elif isinstance(value, list):
        for part in value:
            yield from text_problems(part, path)
    elif isinstance(value, str):
        if value == "":
            yield path, "empty string"
        elif value in ("?", "."):
            yield path, "mmCIF null marker"
        elif value != value.strip():
            yield path, "space at start or end"
        if "�" in value:
            yield path, "replacement character"
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
            yield path, "control character"
        if re.search(r"[\t\n\r]", value):
            yield path, "tab or line break"
        if "  " in value:
            yield path, "two spaces"
    elif value is None:
        yield path, "null"


def mandatory_properties():
    return ("doi", "url", "types", "creators", "titles", "publisher", "publicationYear")


def check_shard(task):
    """Record checks for one pair of shards. Returns findings, counts and [(pdb_id, doi)]."""
    from lxml import etree
    json_path, xml_path, xsd_path, incomplete = task
    schema = etree.XMLSchema(etree.parse(str(xsd_path)))
    findings = Findings()
    counts = collections.Counter()
    owners = []
    json_lines, xml_lines = read_lines(json_path), read_lines(xml_path)
    if len(json_lines) != len(xml_lines):
        findings.add("pairing", None, detail=f"{json_path.name}: {len(json_lines)} JSON lines, {len(xml_lines)} XML lines")
    for json_line, xml_line in zip(json_lines, xml_lines):
        payload, xml_item = json.loads(json_line), json.loads(xml_line)
        if set(payload) != {"data"} or set(payload["data"]) != {"type", "attributes"} or payload["data"]["type"] != "dois":
            findings.add("payload", None, detail=json_line[:200])
            continue
        attributes = payload["data"]["attributes"]
        pdb_id = pdb_id_of(attributes)
        owners.append((pdb_id, attributes.get("doi")))
        counts["records"] += 1
        if xml_item.get("doi") != attributes.get("doi"):
            findings.add("pairing", pdb_id, json=attributes.get("doi"), xml=xml_item.get("doi"))
            continue
        missing = [name for name in mandatory_properties() if not attributes.get(name)]
        if incomplete != bool(missing):
            findings.add("mandatory", pdb_id, missing=missing, incomplete=incomplete)
        for path, problem in text_problems(attributes):
            # Two spaces can be the value as given (organism names, award numbers): a note, not a failure.
            findings.add("note" if problem == "two spaces" else "text", pdb_id, path=path, problem=problem)
        try:
            document = etree.fromstring(xml_item["xml"].encode("utf-8"))
        except etree.XMLSyntaxError as error:
            findings.add("xml", pdb_id, detail=f"not well-formed: {error}")
            continue
        if not incomplete and not schema.validate(document):
            findings.add("xsd", pdb_id, detail=str(schema.error_log.last_error))
        from_xml, unread = xml_to_attributes(xml_item["xml"])
        if unread:
            findings.add("xml", pdb_id, detail=f"elements not read: {unread}")
        json_only = {key: value for key, value in attributes.items() if key not in ("url", "schemaVersion")}
        for key in sorted(set(json_only) | set(from_xml)):
            if canonical(json_only.get(key)) != canonical(from_xml.get(key)):
                findings.add("xml", pdb_id, property=key, json=json_only.get(key), xml=from_xml.get(key))
    return findings, counts, owners


def record_checks(records_dir, holdings, xsd, workers):
    manifest = json.loads((records_dir / "MANIFEST.json").read_text())
    findings = Findings()
    tasks = [(records_dir / shard["json"], records_dir / shard["xml"], xsd, False) for shard in manifest["shards"]]
    tasks += [(records_dir / shard["json"], records_dir / shard["xml"], xsd, True) for shard in manifest["incomplete"]["shards"]]
    counts = collections.Counter()
    published, incomplete = [], []
    with concurrent.futures.ProcessPoolExecutor(workers, multiprocessing.get_context("spawn")) as pool:
        for task, (shard_findings, shard_counts, owners) in zip(tasks, pool.map(check_shard, tasks)):
            findings.merge(shard_findings)
            counts.update(shard_counts)
            (incomplete if task[3] else published).append((task, owners))
            expected = next(shard["records"] for shard in manifest["shards"] + manifest["incomplete"]["shards"] if records_dir / shard["json"] == task[0])
            if expected != len(owners):
                findings.add("manifest", None, detail=f"{task[0].name}: MANIFEST gives {expected} records, the shard has {len(owners)}")
            log(f"records: {task[0].relative_to(records_dir)} checked")
    published_ids = [pdb_id for _, owners in published for pdb_id, _ in owners]
    incomplete_ids = [pdb_id for _, owners in incomplete for pdb_id, _ in owners]
    reasons = [json.loads(line) for line in (records_dir / "incomplete/reasons.jsonl").read_text().splitlines()]
    if sorted(reason["pdb_id"] for reason in reasons) != sorted(incomplete_ids):
        findings.add("manifest", None, detail="incomplete/reasons.jsonl does not name the incomplete records")
    if manifest["records"] != len(published_ids) or manifest["incomplete"]["records"] != len(incomplete_ids):
        findings.add("manifest", None, detail="MANIFEST totals do not agree with the shards")
    statuses = collections.Counter(status for status, _ in holdings.values())
    if manifest["entries"] != dict(statuses) or manifest["snapshot"] != snapshot_date(holdings):
        findings.add("manifest", None, detail=f"MANIFEST entries {manifest['entries']} snapshot {manifest['snapshot']}, holdings {dict(statuses)} {snapshot_date(holdings)}")
    every = published_ids + incomplete_ids
    for pdb_id, count in collections.Counter(every).items():
        if count > 1:
            findings.add("coverage", pdb_id, detail=f"{count} records")
    dois = collections.Counter(doi.lower() for _, owners in published + incomplete for _, doi in owners if doi)
    for doi, count in dois.items():
        if count > 1:
            findings.add("coverage", None, detail=f"DOI {doi} in {count} records")
    for pdb_id in sorted(set(holdings) - set(every)):
        findings.add("coverage", pdb_id, detail="holdings entry without a record")
    for pdb_id in sorted(set(every) - set(holdings)):
        findings.add("coverage", pdb_id, detail="record without a holdings entry")
    errors = (records_dir / "errors.jsonl")
    summary = {
        "records": len(published_ids), "incomplete": len(incomplete_ids), "holdings": dict(statuses),
        "errors_file_lines": len(errors.read_text().splitlines()) if errors.exists() else None,
        "manifest_errors": manifest["errors"],
    }
    return findings, summary


def snapshot_date(holdings):
    newest = max(datetime.datetime.fromisoformat(stamp) for _, stamp in holdings.values())
    return newest.astimezone(datetime.UTC).date().isoformat()


# ---------------------------------------------------------------- the entry file


def source_categories():
    """In the dictionary's letter case: the Python reader selects categories with a case-sensitive match."""
    return ("entry", "database_2", "audit_author", "struct", "pdbx_audit_revision_history", "pdbx_audit_revision_group",
            "pdbx_audit_revision_details", "pdbx_database_status", "struct_keywords", "entity_src_gen", "entity_src_nat",
            "pdbx_entity_src_syn", "em_entity_assembly_naturalsource", "exptl", "entity", "pdbx_contact_author", "pdbx_SG_project",
            "diffrn_detector", "em_imaging", "citation", "citation_author", "pdbx_database_related", "pdbx_initial_refinement_model",
            "pdbx_related_exp_data_set", "pdbx_database_PDB_obs_spr", "struct_ref", "pdbx_deposit_group", "software", "em_software",
            "pdbx_nmr_software", "exptl_crystal_grow", "pdbx_audit_support")


def null(value):
    """mmCIF '?' (unknown) and '.' (not applicable) and blank values are no value."""
    if value is None:
        return None
    value = str(value).strip()
    return None if value in ("", "?", ".") else value


class Source:
    """The categories of one entry file: {category: [{item: value or None}]}, names in lower case."""

    def __init__(self, tables, parser, non_ascii):
        self.tables = tables
        self.parser = parser
        self.non_ascii = non_ascii

    def rows(self, category):
        return self.tables.get(category, [])

    def first(self, category, item):
        return next((row[item] for row in self.rows(category) if row.get(item)), None)


def read_source(path, temp_dir):
    """Read with the wwPDB mmcif package: the C++ reader for ASCII files, the Python reader (UTF-8) otherwise."""
    raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    non_ascii = not raw.isascii()
    with tempfile.TemporaryDirectory(dir=temp_dir) as scratch:
        plain = pathlib.Path(scratch) / "entry.cif"
        plain.write_bytes(raw)
        wanted = list(source_categories())
        if non_ascii:
            from mmcif.io.IoAdapterPy import IoAdapterPy
            containers = IoAdapterPy(raiseExceptions=True, maxInputLineLength=1 << 24).readFile(
                str(plain), enforceAscii=False, selectList=wanted, outDirPath=scratch)
            parser = "IoAdapterPy"
        else:
            from mmcif.io.IoAdapterCore import IoAdapterCore
            containers = IoAdapterCore(raiseExceptions=True, maxInputLineLength=1 << 24).readFile(
                str(plain), enforceAscii=False, selectList=wanted, outDirPath=scratch, logFilePath=str(pathlib.Path(scratch) / "parse.log"))
            parser = "IoAdapterCore"
    if len(containers) != 1:
        raise ValueError(f"{len(containers)} data blocks")
    tables = {}
    container = containers[0]
    for name in container.getObjNameList():
        table = container.getObj(name)
        items = [item.lower() for item in table.getAttributeList()]
        tables[name.lower()] = [dict(zip(items, map(null, row))) for row in table.getRowList()]
    return Source(tables, parser, non_ascii)


# ---------------------------------------------------------------- expected record (CROSSWALK.md)


class Account:
    """Outcome of each source value: kept, or left out with a reason. Keeps a few examples of each reason."""

    def __init__(self, pdb_id):
        self.pdb_id = pdb_id
        self.counts = collections.Counter()
        self.examples = {}

    def note(self, item, outcome, value=None):
        self.counts[(item, outcome)] += 1
        if outcome != "kept":
            self.examples.setdefault((item, outcome), (self.pdb_id, None if value is None else str(value)[:200]))


def clean(value):
    return re.sub(r"\s+", " ", value).strip() if value else value


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def resolve_doi(value):
    value = re.sub(r"(?i)^(?:doi:|https?://(?:dx\.)?doi\.org/)", "", (value or "").strip())
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value) else None


def resolve_pdb_doi(value):
    match = re.fullmatch(r"(?:pdb_0000)?([0-9][A-Za-z0-9]{3})", (value or "").strip())
    return f"10.2210/pdb{match[1].lower()}/pdb" if match else None


def resolve_pmid(value):
    value = (value or "").strip()
    return value if re.fullmatch(r"\d+", value) and value != "0" else None


def resolve_orcid(value):
    value = (value or "").strip()
    return f"https://orcid.org/{value}" if re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", value) else None


def person(name, orcid, account, orcid_item):
    """CROSSWALK.md: split "Family, Given" at the first comma; a name without a comma gets no nameType; ORCID iD as a name identifier."""
    entry = {"name": name}
    if "," in name:
        family, given = (part.strip() for part in name.split(",", 1))
        entry |= {"nameType": "Personal", "familyName": family}
        if given:
            entry["givenName"] = given
    if orcid:
        if identifier := resolve_orcid(orcid):
            entry["nameIdentifiers"] = [{"nameIdentifier": identifier, "nameIdentifierScheme": "ORCID", "schemeUri": "https://orcid.org/"}]
            account.note(orcid_item, "kept")
        else:
            account.note(orcid_item, "left out: not an ORCID iD", orcid)
    return entry


def structure_revisions(src):
    model = "Structure model"
    history = sorted((row for row in src.rows("pdbx_audit_revision_history") if row.get("data_content_type") == model),
                     key=lambda row: as_int(row.get("ordinal")))
    groups, kinds = {}, {}
    for row in src.rows("pdbx_audit_revision_group"):
        if row.get("data_content_type") == model and row.get("group"):
            groups.setdefault(row.get("revision_ordinal"), [])
            if row["group"] not in groups[row["revision_ordinal"]]:
                groups[row["revision_ordinal"]].append(row["group"])
    for row in src.rows("pdbx_audit_revision_details"):
        if row.get("data_content_type") == model and row.get("type"):
            kinds[row.get("revision_ordinal")] = row["type"]
    return [{"date": row.get("revision_date"), "major": row.get("major_revision"), "minor": row.get("minor_revision"),
             "groups": groups.get(row.get("ordinal"), []), "type": kinds.get(row.get("ordinal"))} for row in history]


def published(row):
    journal = row.get("journal_abbrev")
    return bool(journal) and journal.casefold() != "to be published"


def citation_relation(row):
    return "IsSupplementTo" if row.get("id") == "primary" else "References"


def citation_identifiers(row):
    found = []
    if doi := resolve_doi(row.get("pdbx_database_id_doi")):
        found.append({"relatedIdentifier": doi, "relatedIdentifierType": "DOI", "relationType": citation_relation(row)})
    if pmid := resolve_pmid(row.get("pdbx_database_id_pubmed")):
        found.append({"relatedIdentifier": pmid, "relatedIdentifierType": "PMID", "relationType": citation_relation(row)})
    if published(row):
        for identifier in found:
            identifier["resourceTypeGeneral"] = "JournalArticle"
    return found


def expected_record(src, pdb_id, funders):
    """The record that CROSSWALK.md gives for one entry file, and the account of its source values."""
    account = Account(pdb_id)
    record = {}
    entry_id = src.first("entry", "id")
    pdb_row = next((row for row in src.rows("database_2") if row.get("database_id") == "PDB"), {})

    # doi: the DOI of the entry file if it names this entry (the ID DOI, or one with the extended ID), else the ID DOI.
    extended = pdb_row.get("pdbx_database_accession") or f"pdb_0000{(entry_id or '').lower()}"
    doi = resolve_doi(pdb_row.get("pdbx_doi"))
    own_doi = resolve_pdb_doi(entry_id)
    if doi and not ((own_doi and doi.lower() == own_doi) or extended.lower() in doi.lower()):
        account.note("_database_2.pdbx_DOI", "left out: DOI of a different entry", doi)
        doi = None
    elif pdb_row.get("pdbx_doi"):
        account.note("_database_2.pdbx_DOI", "kept" if doi else "left out: not a DOI", pdb_row.get("pdbx_doi"))
    record["doi"] = doi or own_doi

    # url
    record["url"] = f"https://www.wwpdb.org/pdb?id={extended.lower()}"
    record["types"] = {"resourceTypeGeneral": "Dataset", "resourceType": "Structure model"}

    # creators
    creators = []
    for row in sorted(src.rows("audit_author"), key=lambda row: as_int(row.get("pdbx_ordinal"))):
        if row.get("name"):
            account.note("_audit_author.name", "kept")
            creators.append(person(row["name"], row.get("identifier_orcid"), account, "_audit_author.identifier_ORCID"))
    record["creators"] = creators

    # titles
    title = clean(src.first("struct", "title"))
    if title:
        account.note("_struct.title", "kept")
    record["titles"] = [{"title": title}] if title else []
    record["publisher"] = {"name": "Worldwide Protein Data Bank", "publisherIdentifier": "https://ror.org/047qy5748",
                           "publisherIdentifierScheme": "ROR", "schemeUri": "https://ror.org/"}

    # publicationYear
    revisions = structure_revisions(src)
    deposited = src.first("pdbx_database_status", "recvd_initial_deposition_date")
    if revisions and revisions[0]["date"]:
        record["publicationYear"] = revisions[0]["date"][:4]
    elif deposited:
        record["publicationYear"] = deposited[:4]

    # subjects
    subjects, unschemed = [], set()

    def add_unschemed(item, text):
        if text.casefold() in unschemed:
            account.note(item, "left out: repeat (letter case ignored)", text)
            return
        unschemed.add(text.casefold())
        account.note(item, "kept")
        subjects.append({"subject": text})

    for row in src.rows("struct_keywords"):
        if text := clean(row.get("pdbx_keywords")):
            add_unschemed("_struct_keywords.pdbx_keywords", text)
        for part in (row.get("text") or "").split(","):
            if part := clean(part):
                add_unschemed("_struct_keywords.text", part)
    organisms = {}
    for category, name_item, id_item in (("entity_src_gen", "pdbx_gene_src_scientific_name", "pdbx_gene_src_ncbi_taxonomy_id"),
                                         ("entity_src_nat", "pdbx_organism_scientific", "pdbx_ncbi_taxonomy_id"),
                                         ("pdbx_entity_src_syn", "organism_scientific", "ncbi_taxonomy_id"),
                                         ("em_entity_assembly_naturalsource", "organism", "ncbi_tax_id")):
        item = f"_{category}.{id_item}"
        for row in src.rows(category):
            ids = [part.strip() for part in (row.get(id_item) or "").split(",") if part.strip()]
            names = [part.strip() for part in (row.get(name_item) or "").split(",") if part.strip()]
            if not ids:
                if row.get(name_item):
                    account.note(item, "left out: organism name without a taxonomy ID", row.get(name_item))
                continue
            if len(names) != len(ids):
                names = [row.get(name_item)] if len(ids) == 1 else [None] * len(ids)
            for code, name in zip(ids, names):
                if not code.isdigit():
                    account.note(item, "left out: taxonomy ID is not a number", code)
                elif not name:
                    account.note(item, "left out: no organism name for the ID", f"{code} {row.get(name_item)}")
                elif code in organisms:
                    account.note(item, "left out: repeat of a taxonomy ID", code)
                else:
                    organisms[code] = name
                    account.note(item, "kept")
    subjects += [{"subject": name, "subjectScheme": "NCBI Taxonomy", "schemeUri": "https://www.ncbi.nlm.nih.gov/taxonomy",
                  "valueUri": f"https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id={code}", "classificationCode": code}
                 for code, name in organisms.items()]
    methods = []
    for row in src.rows("exptl"):
        if method := clean(row.get("method")):
            if method in methods:
                account.note("_exptl.method", "left out: repeat", method)
                continue
            methods.append(method)
            account.note("_exptl.method", "kept")
    subjects += [{"subject": method, "subjectScheme": "PDBx/mmCIF _exptl.method",
                  "schemeUri": "https://mmcif.wwpdb.org/dictionaries/mmcif_pdbx_v50.dic/Items/_exptl.method.html"} for method in methods]
    for row in src.rows("entity"):
        if name := clean(row.get("pdbx_description")):
            if row.get("type") == "polymer":
                add_unschemed("_entity.pdbx_description", name)
            else:
                account.note("_entity.pdbx_description", f"left out: {row.get('type')} entity", name)
    ec_numbers = []
    for row in src.rows("entity"):
        for part in (row.get("pdbx_ec") or "").split(","):
            if not (part := clean(part.strip())):
                continue
            if not re.fullmatch(r"\d+\.(?:\d+|-)\.(?:\d+|-)\.(?:n?\d+|-)", part):
                account.note("_entity.pdbx_ec", "left out: not an EC number", part)
            elif part in ec_numbers:
                account.note("_entity.pdbx_ec", "left out: repeat", part)
            else:
                ec_numbers.append(part)
                account.note("_entity.pdbx_ec", "kept")
    subjects += [{"subject": ec, "subjectScheme": "Enzyme Commission number", "schemeUri": "https://www.enzyme-database.org/",
                  "valueUri": f"https://www.enzyme-database.org/query.php?ec={ec}", "classificationCode": ec} for ec in ec_numbers]
    record["subjects"] = subjects

    # contributors
    contributors = []
    for row in sorted(src.rows("pdbx_contact_author"), key=lambda row: as_int(row.get("id"))):
        family = row.get("name_last")
        if not family:
            if row.get("name_first"):
                account.note("_pdbx_contact_author.name_last", "left out: contact author without a family name", row.get("name_first"))
            continue
        given = " ".join(part for part in (row.get("name_first"), row.get("name_mi")) if part)
        account.note("_pdbx_contact_author.name_last", "kept")
        contact = {"name": f"{family}, {given}" if given else family, "nameType": "Personal", "familyName": family}
        if given:
            contact["givenName"] = given
        if row.get("identifier_orcid"):
            if identifier := resolve_orcid(row["identifier_orcid"]):
                contact["nameIdentifiers"] = [{"nameIdentifier": identifier, "nameIdentifierScheme": "ORCID", "schemeUri": "https://orcid.org/"}]
                account.note("_pdbx_contact_author.identifier_ORCID", "kept")
            else:
                account.note("_pdbx_contact_author.identifier_ORCID", "left out: not an ORCID iD", row["identifier_orcid"])
        contact["contributorType"] = "ContactPerson"
        contributors.append(contact)
    sites = {"RCSB": "RCSB Protein Data Bank", "PDBE": "Protein Data Bank in Europe", "PDBJ": "Protein Data Bank Japan",
             "BMRB": "Biological Magnetic Resonance Bank", "PDBC": "Protein Data Bank China",
             "BNL": "Protein Data Bank at Brookhaven National Laboratory", "NDB": "Nucleic Acid Database"}
    if site := src.first("pdbx_database_status", "process_site"):
        account.note("_pdbx_database_status.process_site", "kept" if site.upper() in sites else "kept: code not in the site list", site)
        contributors.append({"name": sites.get(site.upper(), site), "nameType": "Organizational", "contributorType": "DataCurator"})
    centers = []
    for row in src.rows("pdbx_sg_project"):
        if center := row.get("full_name_of_center"):
            if center in centers:
                account.note("_pdbx_SG_project.full_name_of_center", "left out: repeat", center)
                continue
            centers.append(center)
            account.note("_pdbx_SG_project.full_name_of_center", "kept")
    contributors += [{"name": center, "nameType": "Organizational", "contributorType": "ResearchGroup"} for center in centers]
    record["contributors"] = contributors

    # dates
    dates = []
    if deposited:
        account.note("_pdbx_database_status.recvd_initial_deposition_date", "kept")
        dates.append({"date": deposited, "dateType": "Submitted"})
    for row in src.rows("pdbx_audit_revision_history"):
        if row.get("data_content_type") != "Structure model":
            account.note("_pdbx_audit_revision_history.revision_date", "left out: not a Structure model revision", row.get("data_content_type"))
    for index, revision in enumerate(revisions):
        if not revision["date"]:
            account.note("_pdbx_audit_revision_history.revision_date", "left out: revision without a date", f"{revision['major']}.{revision['minor']}")
            continue
        account.note("_pdbx_audit_revision_history.revision_date", "kept")
        kind = "Available" if index == 0 else "Withdrawn" if revision["type"] == "Obsolete" else "Updated"
        date = {"date": revision["date"], "dateType": kind}
        if info := ", ".join(revision["groups"]) or revision["type"]:
            date["dateInformation"] = info
        dates.append(date)
    collected = []
    for category, item in (("diffrn_detector", "pdbx_collection_date"), ("em_imaging", "date")):
        for row in src.rows(category):
            if not (value := clean(row.get(item))):
                continue
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                account.note(f"_{category}.{item}", "left out: not a full date", value)
            elif value in collected:
                account.note(f"_{category}.{item}", "left out: repeat", value)
            else:
                collected.append(value)
                account.note(f"_{category}.{item}", "kept")
    dates += [{"date": value, "dateType": "Collected"} for value in collected]
    record["dates"] = dates
    record["language"] = "en"

    # alternateIdentifiers
    record["alternateIdentifiers"] = [{"alternateIdentifier": entry_id, "alternateIdentifierType": "PDB ID"}]
    if extended.lower() != (entry_id or "").lower():
        record["alternateIdentifiers"].append({"alternateIdentifier": extended, "alternateIdentifierType": "Extended PDB ID"})

    # relatedIdentifiers
    related = []

    def add_related(item, value):
        key = canonical(value)
        if key in seen_related:
            account.note(item, "left out: repeat", value["relatedIdentifier"])
            return
        seen_related.add(key)
        account.note(item, "kept")
        related.append(value)

    seen_related = set()
    for row in src.rows("citation"):
        for item, resolver in (("pdbx_database_id_doi", resolve_doi), ("pdbx_database_id_pubmed", resolve_pmid)):
            if row.get(item) and not resolver(row[item]):
                account.note("_citation.pdbx_database_id_DOI" if item.endswith("doi") else "_citation.pdbx_database_id_PubMed", "left out: not an identifier", row[item])
        for identifier in citation_identifiers(row):
            add_related("_citation.pdbx_database_id_DOI" if identifier["relatedIdentifierType"] == "DOI" else "_citation.pdbx_database_id_PubMed", identifier)
    entry_resolvers = {
        "PDB": ("DOI", resolve_pdb_doi),
        "EMDB": ("URL", lambda value: (m := re.fullmatch(r"(?i)(?:EMD-)?(\d{4,})", (value or "").strip())) and f"https://www.ebi.ac.uk/emdb/EMD-{m[1]}"),
        "BMRB": ("URL", lambda value: (m := re.fullmatch(r"(\d+)", (value or "").strip())) and f"https://bmrb.io/data_library/summary/index.php?bmrbId={m[1]}"),
        "SASBDB": ("URL", lambda value: (m := re.fullmatch(r"(SAS[A-Z0-9]+)", (value or "").strip())) and f"https://identifiers.org/sasbdb:{m[1]}"),
    }
    for row in src.rows("pdbx_database_related"):
        database, label, code = row.get("db_name"), row.get("content_type"), row.get("db_id")
        item = "_pdbx_database_related.db_id"
        if not code:
            continue
        if database not in entry_resolvers:
            account.note(item, f"left out: database {database}", code)
        elif label in ("unspecified", "other"):
            account.note(item, f"left out: label {label}", code)
        elif not label:
            account.note(item, "left out: no label", code)
        elif not (identifier := entry_resolvers[database][1](code)):
            account.note(item, f"left out: not a {database} ID", code)
        else:
            add_related(item, {"relatedIdentifier": identifier, "relatedIdentifierType": entry_resolvers[database][0], "relationType": "Other",
                               "relationTypeInformation": label, "resourceTypeGeneral": "Dataset"})
    for row in src.rows("pdbx_initial_refinement_model"):
        if not (code := clean(row.get("accession_code"))):
            continue
        item = "_pdbx_initial_refinement_model.accession_code"
        if row.get("source_name") != "PDB":
            account.note(item, f"left out: source {row.get('source_name')}", code)
        elif not (identifier := resolve_pdb_doi(code)):
            account.note(item, "left out: not a PDB ID", code)
        else:
            add_related(item, {"relatedIdentifier": identifier, "relatedIdentifierType": "DOI", "relationType": "IsDerivedFrom", "resourceTypeGeneral": "Dataset"})
    for row in src.rows("pdbx_related_exp_data_set"):
        if not (reference := clean(row.get("data_reference"))):
            continue
        item = "_pdbx_related_exp_data_set.data_reference"
        if identifier := resolve_doi(reference):
            add_related(item, {"relatedIdentifier": identifier, "relatedIdentifierType": "DOI", "relationType": "IsDerivedFrom", "resourceTypeGeneral": "Dataset"})
        else:
            account.note(item, "left out: not a DOI", reference)
    own = (entry_id or "").upper()
    for row in src.rows("pdbx_database_pdb_obs_spr"):
        new = (row.get("pdb_id") or "").upper()
        olds = (row.get("replace_pdb_id") or "").upper().split()
        item = "_pdbx_database_PDB_obs_spr"
        news = new.split()
        # CROSSWALK.md: SPRSDE gives Obsoletes, OBSLTE gives IsObsoletedBy. Both ID items can hold more than one ID.
        if row.get("id") == "SPRSDE" and own in news:
            targets = [(old, "Obsoletes") for old in olds]
        elif row.get("id") == "OBSLTE" and own in olds:
            targets = [(code, "IsObsoletedBy") for code in news if code != "NONE"]
            if "NONE" in news:
                account.note(item, "left out: no successor (NONE)", new)
        else:
            account.note(item, "left out: row does not link this entry", f"{row.get('id')} {new} {olds}")
            continue
        for code, relation in targets:
            if identifier := resolve_pdb_doi(code):
                add_related(item, {"relatedIdentifier": identifier, "relatedIdentifierType": "DOI", "relationType": relation, "resourceTypeGeneral": "Dataset"})
            else:
                account.note(item, "left out: not a PDB ID", code)
    pattern = r"(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})(?:-\d+)?"
    for row in src.rows("struct_ref"):
        if not (accession := clean(row.get("pdbx_db_accession"))):
            continue
        item = "_struct_ref.pdbx_db_accession"
        if row.get("db_name") != "UNP":
            account.note(item, f"left out: database {row.get('db_name')}", accession)
        elif not re.fullmatch(pattern, accession.upper()):
            account.note(item, "left out: not a UniProt accession", accession)
        else:
            add_related(item, {"relatedIdentifier": f"https://www.uniprot.org/uniprotkb/{accession.upper()}", "relatedIdentifierType": "URL",
                               "relationType": "References", "resourceTypeGeneral": "Dataset"})
    record["relatedIdentifiers"] = related

    # relatedItems
    items = []
    authors = src.rows("citation_author")
    for row in src.rows("citation"):
        if citation_identifiers(row):
            continue
        item = "_citation (no DOI or PubMed ID)"
        if not row.get("title"):
            account.note(item, "left out: no title", row.get("id"))
            continue
        if (row.get("journal_abbrev") or "").casefold() == "to be published":
            account.note(item, "left out: to be published", row.get("id"))
            continue
        # CROSSWALK.md leaves out only "To be published" citations, so a citation without a journal stays.
        account.note(item, "kept" if row.get("journal_abbrev") else "kept: no journal", f"{row.get('id')} book={row.get('book_title')}")
        value = {"relationType": citation_relation(row), "relatedItemType": "BookChapter" if row.get("book_title") else "JournalArticle"}
        names = sorted((author for author in authors if author.get("citation_id") == row.get("id") and author.get("name")),
                       key=lambda author: as_int(author.get("ordinal")))
        if names:
            value["creators"] = [person(author["name"], None, account, None) for author in names]
        value["titles"] = [{"title": clean(row["title"])}]
        for source, target in (("year", "publicationYear"), ("journal_volume", "volume"), ("journal_issue", "issue"),
                               ("page_first", "firstPage"), ("page_last", "lastPage")):
            if row.get(source):
                value[target] = row[source]
        items.append(value)
    for row in src.rows("pdbx_deposit_group"):
        if group := row.get("group_id"):
            account.note("_pdbx_deposit_group.group_id", "kept")
            items.append({"relationType": "IsPartOf", "relatedItemType": "Collection", "titles": [{"title": clean(row.get("group_title")) or group}],
                          "number": group, "numberType": "Other"})
    software = set()
    for category in ("software", "em_software", "pdbx_nmr_software"):
        for row in src.rows(category):
            name, version = clean(row.get("name")), clean(row.get("version"))
            if not name:
                if version:
                    account.note(f"_{category}.version", "left out: version without a name", version)
                continue
            if (name.casefold(), version) in software:
                account.note(f"_{category}.name", "left out: repeat (letter case ignored)", f"{name} {version}")
                continue
            software.add((name.casefold(), version))
            account.note(f"_{category}.name", "kept")
            value = {"relationType": "IsCompiledBy", "relatedItemType": "Software", "titles": [{"title": name}]}
            if version:
                value["edition"] = version
            items.append(value)
    microscopes = []
    for row in src.rows("em_imaging"):
        if model := clean(row.get("microscope_model")):
            if model in microscopes:
                account.note("_em_imaging.microscope_model", "left out: repeat", model)
                continue
            microscopes.append(model)
            account.note("_em_imaging.microscope_model", "kept")
    items += [{"relationType": "IsCollectedBy", "relatedItemType": "Instrument", "titles": [{"title": model}]} for model in microscopes]
    record["relatedItems"] = items

    # formats, version, rights
    record["formats"] = ["PDBx/mmCIF", "PDBML"] + (["PDB"] if src.first("pdbx_database_status", "pdb_format_compatible") == "Y" else [])
    if revisions:
        last = revisions[-1]
        if last["major"] and last["minor"]:
            record["version"] = f"{last['major']}.{last['minor']}"
        else:
            account.note("_pdbx_audit_revision_history.major_revision", "left out: no version number", f"{last['major']}.{last['minor']}")
    record["rightsList"] = [{"rights": "Creative Commons Zero v1.0 Universal", "rightsUri": "https://creativecommons.org/publicdomain/zero/1.0/legalcode",
                             "rightsIdentifier": "cc0-1.0", "rightsIdentifierScheme": "SPDX", "schemeUri": "https://spdx.org/licenses/"}]

    # descriptions
    details = []
    for row in src.rows("exptl_crystal_grow"):
        if text := clean(row.get("pdbx_details")):
            if text in details:
                account.note("_exptl_crystal_grow.pdbx_details", "left out: repeat", text)
                continue
            details.append(text)
            account.note("_exptl_crystal_grow.pdbx_details", "kept")
    record["descriptions"] = [{"description": text, "descriptionType": "Methods"} for text in details]

    # fundingReferences
    references, seen_references = [], set()
    for row in sorted(src.rows("pdbx_audit_support"), key=lambda row: as_int(row.get("ordinal"))):
        name = clean(row.get("funding_organization"))
        item = "_pdbx_audit_support.funding_organization"
        if not name:
            if row.get("grant_number"):
                account.note("_pdbx_audit_support.grant_number", "left out: grant without a funder", row["grant_number"])
            continue
        funder = funders.get(name)
        if funder and funder["status"] == "excluded":
            account.note(item, "left out: generic value (funder table status excluded)", name)
            continue
        reference = {"funderName": name}
        if funder and funder["ror_id"]:
            reference |= {"funderIdentifier": funder["ror_id"], "funderIdentifierType": "ROR", "schemeUri": "https://ror.org/"}
        if row.get("grant_number"):
            reference["awardNumber"] = row["grant_number"]
        key = canonical(reference)
        if key in seen_references:
            account.note(item, "left out: repeat", name)
            continue
        seen_references.add(key)
        outcome = "kept with a ROR ID" if "funderIdentifier" in reference else "kept without a ROR ID" if funder else "kept: name not in the funder table"
        account.note(item, outcome, name)
        if row.get("grant_number"):
            account.note("_pdbx_audit_support.grant_number", "kept")
        references.append(reference)
    record["fundingReferences"] = references
    record["schemaVersion"] = "http://datacite.org/schema/kernel-4"

    record = {key: value for key, value in record.items() if value not in (None, [], "")}
    missing = [name for name in ("doi", "url", "types", "creators", "titles", "publisher", "publicationYear") if name not in record]
    return record, missing, account


# ---------------------------------------------------------------- compare


def difference(expected, actual):
    """A short description of how two property values differ."""
    if isinstance(expected, list) and isinstance(actual, list):
        expected_keys, actual_keys = [canonical(value) for value in expected], [canonical(value) for value in actual]
        only_expected = [value for value, key in zip(expected, expected_keys) if key not in actual_keys]
        only_actual = [value for value, key in zip(actual, actual_keys) if key not in expected_keys]
        detail = {"expected_count": len(expected), "actual_count": len(actual)}
        if only_expected:
            detail["not_in_record"] = only_expected[:5]
        if only_actual:
            detail["not_expected"] = only_actual[:5]
        if not only_expected and not only_actual:
            detail["problem"] = "same values, different order or repeats"
        return detail
    return {"expected": expected, "actual": actual}


def check_entry(task):
    pdb_id, path, record_line, incomplete, funders_path, temp_dir = task
    funders = load_funders(funders_path)
    findings = Findings()
    properties = collections.Counter()
    try:
        src = read_source(path, temp_dir)
    except Exception as error:  # noqa: BLE001
        findings.add("source_read", pdb_id, detail=f"{type(error).__name__}: {error}")
        return findings, properties, collections.Counter(), {}, collections.Counter()
    stats = collections.Counter({f"parser {src.parser}": 1, "non-ASCII entry file": int(src.non_ascii)})
    if src.non_ascii:
        findings.add("note", pdb_id, detail="entry file has non-ASCII text, read with the Python reader")
    expected, missing, account = expected_record(src, pdb_id, funders)
    if (src.first("entry", "id") or "").lower() != pdb_id:
        findings.add("property", pdb_id, property="entry id", detail=f"_entry.id is {src.first('entry', 'id')}")
    if record_line is None:
        findings.add("coverage", pdb_id, detail="no record for this entry")
        return findings, properties, account.counts, account.examples, stats
    actual = json.loads(record_line)["data"]["attributes"]
    if bool(missing) != incomplete:
        findings.add("mandatory", pdb_id, detail=f"rules give missing {missing}, record is {'incomplete' if incomplete else 'published'}")
    # A DOI that is not the DOI of this entry ID registers a wrong DOI, also if the entry file gives it (7QNP).
    if actual.get("doi", "").lower() != f"10.2210/pdb{pdb_id}/pdb":
        findings.add("doi", pdb_id, record=actual.get("doi"), detail=f"expected {expected.get('doi')}")
    if list(actual) != [key for key in expected if key in actual]:
        findings.add("property", pdb_id, property="(order)", detail=f"record keys {list(actual)}")
    for key in dict.fromkeys(list(expected) + list(actual)):
        properties[(key, "checked")] += 1
        if canonical(expected.get(key)) == canonical(actual.get(key)):
            properties[(key, "equal")] += 1
        else:
            properties[(key, "different")] += 1
            findings.add("property", pdb_id, property=key, **difference(expected.get(key), actual.get(key)))
    return findings, properties, account.counts, account.examples, stats


@functools.cache
def load_funders(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return {row["funding_organization"]: {"status": row["status"], "ror_id": row["ror_id"]} for row in csv.DictReader(handle)}


def load_records(records_dir):
    """{pdb_id: (json_line, incomplete)} for all records."""
    manifest = json.loads((records_dir / "MANIFEST.json").read_text())
    records = {}
    for shards, incomplete in ((manifest["shards"], False), (manifest["incomplete"]["shards"], True)):
        for shard in shards:
            for line in read_lines(records_dir / shard["json"]):
                records[pdb_id_of(json.loads(line)["data"]["attributes"])] = (line, incomplete)
    return records


def entry_batches(args, holdings, wanted):
    """Yield lists of (pdb_id, path) whose files are on disk; bucket files are deleted after each batch."""
    ordered = sorted(wanted)
    if args.mirror:
        for chunk in itertools.batched(ordered, args.chunk):
            yield [(pdb_id, args.mirror / archive_path(pdb_id, holdings[pdb_id][0])) for pdb_id in chunk]
        return
    from huggingface_hub import HfApi
    api = HfApi()
    listing = {item.path: item for item in api.list_bucket_tree(args.bucket, prefix=f"{args.prefix}/data/structures/", recursive=True) if item.type == "file"}
    staging = args.work_dir / "mirror"

    def download(chunk):
        pairs, files = [], []
        for pdb_id in chunk:
            key = f"{args.prefix}/{archive_path(pdb_id, holdings[pdb_id][0])}"
            path = staging / archive_path(pdb_id, holdings[pdb_id][0])
            if key in listing:
                files.append((listing[key], str(path)))
            pairs.append((pdb_id, path))
        api.download_bucket_files(args.bucket, files)
        return pairs

    with concurrent.futures.ThreadPoolExecutor(1) as prefetch:
        chunks = iter(itertools.batched(ordered, args.chunk))
        pending = prefetch.submit(download, next(chunks, ()))
        for chunk in chunks:
            ready, pending = pending.result(), prefetch.submit(download, chunk)
            yield ready
            for _, path in ready:
                path.unlink(missing_ok=True)
        ready = pending.result()
        yield ready
        for _, path in ready:
            path.unlink(missing_ok=True)


def source_checks(args, holdings, records, wanted):
    findings = Findings()
    properties, accounting, stats = collections.Counter(), collections.Counter(), collections.Counter()
    examples = {}
    temp_dir = args.work_dir / "parse"
    temp_dir.mkdir(parents=True, exist_ok=True)
    done = 0
    with concurrent.futures.ProcessPoolExecutor(args.workers, multiprocessing.get_context("spawn")) as pool:
        for batch in entry_batches(args, holdings, wanted):
            tasks = []
            for pdb_id, path in batch:
                if not path.exists():
                    findings.add("source_read", pdb_id, detail=f"no entry file at {path}")
                    continue
                line, incomplete = records.get(pdb_id, (None, False))
                tasks.append((pdb_id, path, line, incomplete, args.funders, temp_dir))
            for entry_findings, entry_properties, entry_counts, entry_examples, entry_stats in pool.map(check_entry, tasks, chunksize=16):
                findings.merge(entry_findings)
                properties.update(entry_properties)
                accounting.update(entry_counts)
                stats.update(entry_stats)
                for key, example in entry_examples.items():
                    if len(kept := examples.setdefault(key, [])) < 5:
                        kept.append(example)
            done += len(batch)
            log(f"source: {done}/{len(wanted)} entries checked, {sum(findings.counts.values())} findings")
    by_property = {}
    for (key, outcome), count in sorted(properties.items()):
        by_property.setdefault(key, {})[outcome] = count
    by_item = {}
    for (item, outcome), count in sorted(accounting.items()):
        by_item.setdefault(item, {})[outcome] = count
    for (item, outcome), found in examples.items():
        by_item[item].setdefault("examples", {})[outcome] = found
    return findings, {"entries": len(wanted), "properties": by_property, "accounting": by_item, "files": dict(stats)}


# ---------------------------------------------------------------- main


def main():
    args = parse_args()
    logging.basicConfig(format="%(message)s", stream=sys.stderr)
    logging.getLogger("fidelity").setLevel(logging.INFO if args.verbose else logging.WARNING)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.out.mkdir(parents=True, exist_ok=True)
    from huggingface_hub import HfApi
    from huggingface_hub.utils import disable_progress_bars
    disable_progress_bars()
    records_dir = args.records or fetch_dataset(args.dataset, args.work_dir / "records")
    holdings_dir = args.holdings or fetch_holdings(HfApi(), args.bucket, args.prefix, args.work_dir / "holdings")
    holdings = read_holdings(holdings_dir)
    findings, record_summary = record_checks(records_dir, holdings, args.xsd, args.workers)
    log(f"records: {record_summary}, findings {dict(findings.counts)}")
    report = {
        "dataset": args.dataset or str(args.records),
        "snapshot": snapshot_date(holdings),
        "funders_sha256": hashlib.sha256(args.funders.read_bytes()).hexdigest(),
        "record_checks": record_summary,
    }
    if not args.skip_source:
        records = load_records(records_dir)
        wanted = set(holdings)
        if args.entries:
            wanted &= {line.strip().lower() for line in args.entries.read_text().splitlines() if line.strip()}
        source_findings, report["source_checks"] = source_checks(args, holdings, records, wanted)
        findings.merge(source_findings)
    report["findings"] = dict(sorted(findings.counts.items()))
    (args.out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with gzip.GzipFile(args.out / "findings.jsonl.gz", "wb", mtime=0) as handle:
        for line in findings.lines:
            handle.write((json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8"))
    if args.publish:
        from huggingface_hub import HfApi
        namespace, bucket, path = args.publish.strip("/").split("/", 2)
        HfApi().batch_bucket_files(f"{namespace}/{bucket}", add=[(str(args.out / name), f"{path}/{name}") for name in ("report.json", "findings.jsonl.gz")])
    log(f"findings: {report['findings']}")
    sys.exit(1 if set(findings.counts) - {"note"} else 0)


if __name__ == "__main__":
    main()
