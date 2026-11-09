"""DataCite Metadata Schema 4.7 records from PDBx/mmCIF entry files.

fetch         Download entry files from a wwPDB archive mirror.
convert       Make DataCite JSON and XML records with a YAML mapping.
validate      Validate DataCite XML records against the XSD.
funder-usage  Count the PDB funding rows for each funder string (PDBj Mine SQL service).
funder-table  Map the PDB funder strings to ROR IDs.
"""

import argparse
import json
import pathlib
import sys

import xmlschema

from . import archive, crosswalk, funders


def lookup_arg(text):
    name, _, path = text.partition("=")
    if not name or not path:
        raise argparse.ArgumentTypeError(f"expected NAME=PATH, got {text!r}")
    return name, pathlib.Path(path)


def url_root(value):
    return value if value.endswith("/") else value + "/"


def parse_args():
    parser = argparse.ArgumentParser(prog="pdb-datacite", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    fetch = commands.add_parser("fetch", help="download entry files from a wwPDB archive mirror")
    fetch.add_argument("ids", nargs="+", help="PDB IDs (4-character)")
    fetch.add_argument("-o", "--out-dir", required=True, type=pathlib.Path)
    fetch.add_argument("--archive-url", required=True, type=url_root,
                       help="wwPDB archive root, for example https://files.wwpdb.org/pub/pdb/")
    fetch.add_argument("--delay", type=float, default=0.2, help="seconds between requests")

    convert = commands.add_parser("convert", help="make DataCite records with a YAML mapping")
    convert.add_argument("inputs", nargs="+", type=pathlib.Path, help="mmCIF files (.cif or .cif.gz)")
    convert.add_argument("--mapping", required=True, type=pathlib.Path, help="data/mapping.yaml")
    convert.add_argument("--lookup", action="append", default=[], type=lookup_arg, metavar="NAME=PATH",
                         help="a lookup file that the mapping declares, such as funders=data/funder_ror.csv; repeatable")
    convert.add_argument("-o", "--out-dir", required=True, type=pathlib.Path)
    convert.add_argument("--incomplete-dir", required=True, type=pathlib.Path,
                         help="directory for the records that have no value for a mandatory property")
    convert.add_argument("--format", required=True, nargs="+", choices=["json", "xml"], help="output formats")

    validate = commands.add_parser("validate", help="validate DataCite XML files against the XSD")
    validate.add_argument("files", nargs="+", type=pathlib.Path)
    validate.add_argument("--xsd", required=True, type=pathlib.Path, help="path to metadata.xsd")

    usage = commands.add_parser("funder-usage", help="count the PDB funding rows for each funder string")
    usage.add_argument("-o", "--out", required=True, type=pathlib.Path, help="output CSV")

    table = commands.add_parser("funder-table", help="map the PDB funder strings to ROR IDs",
                                description=funders.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    table.add_argument("--dictionary", required=True, type=pathlib.Path, help="mmcif_pdbx_v50.dic(.gz)")
    table.add_argument("--ror-dump", required=True, type=pathlib.Path, help="ROR schema v2 JSON dump")
    table.add_argument("--usage", required=True, type=pathlib.Path, help="CSV from funder-usage")
    table.add_argument("--decisions", type=pathlib.Path, help="CSV of reviewed decisions")
    table.add_argument("-o", "--out", required=True, type=pathlib.Path, help="output CSV")

    args = parser.parse_args()
    if args.command == "convert" and len(dict(args.lookup)) < len(args.lookup):
        convert.error("--lookup: a name is given more than once")
    return args


def run_convert(args):
    try:
        walk = crosswalk.load(args.mapping, dict(args.lookup))
    except crosswalk.MappingError as error:
        sys.exit(f"{args.mapping}: {error}")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.incomplete_dir.mkdir(parents=True, exist_ok=True)
    reasons = []
    for path in args.inputs:
        try:
            conversion = walk.convert(path)
            record = conversion.record
            outputs = {}
            if "json" in args.format:
                payload = {"data": {"type": "dois", "attributes": record}}
                outputs["json"] = json.dumps(payload, indent=2, ensure_ascii=False)
            if "xml" in args.format:
                outputs["xml"] = crosswalk.to_xml(record)
        except Exception as error:  # noqa: BLE001
            print(f"{path}: {type(error).__name__}: {error}", file=sys.stderr)
            continue
        # doi is mandatory, so only an incomplete record can lack it.
        stem = record["doi"].split("/")[1] if "doi" in record else path.name.removesuffix(".gz").removesuffix(".cif")
        out_dir = args.incomplete_dir if conversion.missing else args.out_dir
        for suffix, text in outputs.items():
            (out_dir / f"{stem}.{suffix}").write_text(text + "\n", encoding="utf-8")
        if conversion.missing:
            reasons.append({"input": str(path), "files": [f"{stem}.{suffix}" for suffix in outputs],
                            "missing": list(conversion.missing)})
    (args.incomplete_dir / "reasons.jsonl").write_text(
        "".join(json.dumps(reason, ensure_ascii=False) + "\n" for reason in reasons), encoding="utf-8")


def run_validate(args):
    schema = xmlschema.XMLSchema(str(args.xsd))
    failed = 0
    for path in args.files:
        errors = list(schema.iter_errors(str(path)))
        for error in errors[:5]:
            print(f"{path}: {error.reason} at {error.path}", file=sys.stderr)
        failed += bool(errors)
    sys.exit(1 if failed else 0)


def main():
    args = parse_args()
    if args.command == "fetch":
        archive.fetch_ids(args.ids, args.out_dir, args.archive_url, args.delay)
    elif args.command == "convert":
        run_convert(args)
    elif args.command == "validate":
        run_validate(args)
    elif args.command == "funder-usage":
        funders.fetch_usage(args.out)
    elif args.command == "funder-table":
        funders.build_table(args.dictionary, args.ror_dump, args.usage, args.decisions, args.out)
