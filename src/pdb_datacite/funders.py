"""The funder lookup table: PDB funding_organization strings mapped to ROR IDs, conservatively.

fetch_usage() counts the PDB funding rows for each string with the PDBj Mine
SQL service. build_table() maps each string and writes the table.

Rules, in order:
1. Generic values ("Not funded", "Other government", ...) are excluded.
2. A name in the PDBx dictionary list maps when its name (or, for
   "Parent/Child" names, the child part) is exactly equal, after
   normalisation, to a name, label or alias of one ROR record in the same
   country. If several ROR records qualify, the name goes to review.
3. A name that is not in the dictionary list inherits the mapping of a
   dictionary name when both have the same base name (text before the
   trailing parenthetical), the free-text parenthetical adds nothing that the
   dictionary parenthetical lacks, and the free-text name repeats any country
   that the dictionary name gives. Exactly one dictionary name must qualify.
4. Other free-text names use rule 2 only when they give a country in a
   trailing parenthetical, for example "Medical Research Council (United
   Kingdom)". Free text without a country is not matched.
5. A row in the decisions file overrides rules 2 to 4.

Anything else stays unmapped, with candidate ROR records listed for review.
"""

import collections
import csv
import gzip
import json
import re
import shlex
import unicodedata

import requests


def fetch_usage(out):
    """Write the PDB funding rows and entries for each funding_organization string to the CSV out."""
    response = requests.get(
        "https://pdbj.org/rest/newweb/search/sql",
        params={
            "q": "select funding_organization, count(*) as n_rows, count(distinct pdbid) as n_entries "
                 "from pdbj.pdbx_audit_support group by 1 order by 2 desc",
            "format": "json",
        },
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if "results" not in payload:
        raise RuntimeError(payload)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["funding_organization", "n_rows", "n_entries"])
        for name, n_rows, n_entries in payload["results"]:
            writer.writerow(["" if name is None else name, n_rows, n_entries])


def normalise(text):
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("&", " and ").replace("'", "").replace("’", "")
    text = re.sub(r"[^\w]+", " ", text)
    text = re.sub(r"^the ", "", text.strip())
    return re.sub(r"\s+", " ", text).strip()


def split_parenthetical(name):
    match = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", name)
    if not match:
        return name.strip(), []
    tokens = [token.strip() for token in re.split(r"[,/]", match.group(2)) if token.strip()]
    return match.group(1).strip(), tokens


def read_text(path):
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8")


def read_dictionary_funders(path):
    text = read_text(path)
    frame = re.search(r"^save__pdbx_audit_support\.funding_organization\s*$(.*?)^save_\s*$", text, re.DOTALL | re.MULTILINE)
    funders = {}
    for line in frame.group(1).splitlines():
        line = line.strip()
        if not line.startswith('"_pdbx_audit_support.funding_organization"'):
            continue
        parts = shlex.split(line)
        country = parts[2] if len(parts) > 2 and parts[2] != "." else ""
        funders[parts[1]] = country
    return funders


def read_usage(path):
    with path.open(encoding="utf-8") as handle:
        return {row["funding_organization"]: int(row["n_rows"]) for row in csv.DictReader(handle)}


def read_decisions(path):
    if path is None:
        return {}
    with path.open(encoding="utf-8") as handle:
        return {row["funding_organization"]: row for row in csv.DictReader(handle)}


def country_key(country):
    aliases = {
        "korea, republic of": "south korea",
        "russian federation": "russia",
        "czech republic": "czechia",
        "netherlands": "the netherlands",
        "turkey": "türkiye",
        "korea": "south korea",
        "usa": "united states",
        "us": "united states",
        "uk": "united kingdom",
    }
    key = country.strip().casefold()
    return aliases.get(key, key)


def country_token(tokens, country_keys):
    return next((token for token in tokens if country_key(token) in country_keys), "")


def inherits(tokens, parent, country_keys):
    parent_tokens = split_parenthetical(parent)[1]
    parent_keys = {country_key(token) for token in parent_tokens}
    own_keys = {country_key(token) for token in tokens}
    if not own_keys <= parent_keys:
        return False
    parent_country = country_token(parent_tokens, country_keys)
    return not parent_country or country_key(parent_country) in own_keys


def load_ror(path):
    records = {}
    by_name = collections.defaultdict(set)
    by_acronym = collections.defaultdict(set)
    with path.open(encoding="utf-8") as handle:
        dump = json.load(handle)
    for record in dump:
        details = record["locations"][0]["geonames_details"] if record.get("locations") else {}
        fundref = []
        for external in record.get("external_ids", []):
            if external["type"] == "fundref":
                fundref = [external["preferred"]] if external.get("preferred") else external["all"]
        display = next(n["value"] for n in record["names"] if "ror_display" in n["types"])
        records[record["id"]] = {
            "name": display,
            "status": record["status"],
            "country": details.get("country_name", ""),
            "fundref": fundref,
        }
        for name in record["names"]:
            if "acronym" in name["types"]:
                by_acronym[name["value"].casefold()].add(record["id"])
            else:
                by_name[normalise(name["value"])].add(record["id"])
    return records, by_name, by_acronym


def name_candidates(name):
    base, tokens = split_parenthetical(name)
    forms = [base]
    if "/" in base:
        forms.append(base.rsplit("/", 1)[1].strip())
    return base, tokens, forms


def match_dictionary_name(name, country, ror):
    records, by_name, by_acronym = ror
    _, tokens, forms = name_candidates(name)
    acronyms = {token.casefold() for token in tokens}
    for form in reversed(forms):
        ids = by_name.get(normalise(form), set())
        if not ids:
            continue
        if country and country != "European Union":
            ids = {i for i in ids if country_key(records[i]["country"]) == country_key(country)}
        active = {i for i in ids if records[i]["status"] == "active"}
        if len(ids) > 1 and len(active) == 1:
            ids = active
        if len(ids) == 1:
            ror_id = next(iter(ids))
            acronym_ok = any(ror_id in by_acronym.get(a, set()) for a in acronyms)
            return "exact", ror_id, f"name={form!r}" + ("; acronym agrees" if acronym_ok else "")
        if len(ids) > 1:
            return "review", "", "several ROR records: " + " ".join(sorted(ids))
    candidates = set()
    for token in tokens:
        candidates |= by_acronym.get(token.casefold(), set())
    if country and country != "European Union":
        candidates = {i for i in candidates if country_key(records[i]["country"]) == country_key(country)}
    if candidates:
        return "review", "", "acronym candidates: " + " ".join(sorted(candidates))
    return "review", "", "no exact ROR name"


def build_table(dictionary_path, ror_dump, usage_path, decisions_path, out):
    """Write the funder table to the CSV out. decisions_path can be None."""
    dictionary = read_dictionary_funders(dictionary_path)
    usage = read_usage(usage_path)
    decisions = read_decisions(decisions_path)
    ror = load_ror(ror_dump)
    records = ror[0]
    excluded = {"not funded", "other government", "other private", "other", ""}
    country_keys = {country_key(r["country"]) for r in records.values() if r["country"]}

    base_index = collections.defaultdict(set)
    for name in dictionary:
        base_index[normalise(split_parenthetical(name)[0])].add(name)

    results = {}
    for name, country in dictionary.items():
        if normalise(name) in excluded:
            results[name] = ("excluded", "", "generic value, not a funder")
        else:
            results[name] = match_dictionary_name(name, country, ror)
        decision = decisions.get(name)
        if decision:
            status = "reviewed" if decision["ror_id"] else "reviewed_none"
            results[name] = (status, decision["ror_id"], decision["note"])

    rows = []
    for name in sorted(set(dictionary) | set(usage), key=lambda n: (-usage.get(n, 0), n)):
        in_dictionary = name in dictionary
        via = ""
        if in_dictionary:
            status, ror_id, note = results[name]
        elif normalise(name) in excluded:
            status, ror_id, note = "excluded", "", "generic value, not a funder"
        else:
            base, tokens = split_parenthetical(name)
            parents = [p for p in base_index.get(normalise(base), set()) if inherits(tokens, p, country_keys)]
            if len(parents) == 1:
                via = parents[0]
                parent_status, ror_id, parent_note = results[via]
                status = "variant" if parent_status in ("exact", "reviewed") else "variant_unmapped"
                note = "same base name as dictionary value"
                if parent_note.startswith("interpretive"):
                    note = f"{parent_note}; {note}"
            elif country_token(tokens, country_keys):
                status, ror_id, note = match_dictionary_name(name, country_token(tokens, country_keys), ror)
                status = "legacy_exact" if status == "exact" else "legacy_unmapped"
            else:
                status, ror_id, note = "legacy_unmapped", "", "free text without a country; not matched"
        decision = decisions.get(name)
        if decision and not in_dictionary:
            status = "reviewed" if decision["ror_id"] else "reviewed_none"
            ror_id = decision["ror_id"]
            note = decision["note"]
            via = ""
        record = records.get(ror_id, {})
        rows.append({
            "funding_organization": name,
            "in_dictionary": "Y" if in_dictionary else "N",
            "pdb_country": dictionary.get(name, ""),
            "usage_rows": usage.get(name, 0),
            "status": status,
            "ror_id": ror_id,
            "ror_name": record.get("name", ""),
            "ror_status": record.get("status", ""),
            "ror_country": record.get("country", ""),
            "fundref_ids": " ".join(record.get("fundref", [])),
            "via_dictionary_name": via,
            "note": note,
        })

    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
