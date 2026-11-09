"""The sample entries in tests/samples/mmcif/ give the records in tests/samples/datacite/, byte for byte."""

import pathlib
import sys

import xmlschema

from pdb_datacite import cli


def repo_root():
    return pathlib.Path(__file__).resolve().parent.parent


def test_convert_gives_the_sample_records(tmp_path, monkeypatch):
    root = repo_root()
    inputs = sorted((root / "tests/samples/mmcif").glob("*.cif"))
    monkeypatch.setattr(sys, "argv", [
        "pdb-datacite", "convert", *map(str, inputs),
        "--mapping", str(root / "data/mapping.yaml"),
        "--lookup", f"funders={root / 'data/funder_ror.csv'}",
        "-o", str(tmp_path / "records"),
        "--incomplete-dir", str(tmp_path / "incomplete"),
        "--format", "json", "xml",
    ])
    cli.main()
    expected = {path.name: path.read_bytes() for path in (root / "tests/samples/datacite").iterdir()}
    made = {path.name: path.read_bytes() for path in (tmp_path / "records").iterdir()}
    assert len(expected) == 2 * len(inputs)
    assert made == expected
    assert sorted(path.name for path in (tmp_path / "incomplete").iterdir()) == ["reasons.jsonl"]
    assert (tmp_path / "incomplete/reasons.jsonl").read_text() == ""


def test_sample_records_are_valid_for_the_xsd():
    root = repo_root()
    schema = xmlschema.XMLSchema(str(root / "schema/datacite-4.7/metadata.xsd"))
    records = sorted((root / "tests/samples/datacite").glob("*.xml"))
    assert records
    for path in records:
        assert list(schema.iter_errors(str(path))) == [], path.name
