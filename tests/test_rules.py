"""Rules that the full-archive fidelity check found broken (2026-11-09). Each test uses a small entry file."""

import pathlib

from pdb_datacite import crosswalk


def repo_root():
    return pathlib.Path(__file__).resolve().parent.parent


def convert(tmp_path, pdb_id, body, pdbx_doi=None):
    """The record for an entry file with the _database_2 row of pdb_id and the given categories."""
    doi = pdbx_doi or f"10.2210/pdb{pdb_id.lower()}/pdb"
    text = f"""data_{pdb_id}
_entry.id {pdb_id}
#
loop_
_database_2.database_id
_database_2.database_code
_database_2.pdbx_database_accession
_database_2.pdbx_DOI
PDB {pdb_id} pdb_0000{pdb_id.lower()} {doi}
#
_struct.title 'Test entry'
#
{body}
"""
    path = tmp_path / f"{pdb_id.lower()}.cif"
    path.write_text(text, encoding="utf-8")
    root = repo_root()
    walk = crosswalk.load(root / "data/mapping.yaml", {"funders": root / "data/funder_ror.csv"})
    return walk.convert(path).record


def related(record, relation):
    return [item["relatedIdentifier"] for item in record.get("relatedIdentifiers", []) if item["relationType"] == relation]


def test_doi_of_another_entry_gives_the_doi_of_the_entry_id(tmp_path):
    # The entry file of 7QNP gives 10.2210/pdb7np/pdb, a DOI that does not exist.
    assert convert(tmp_path, "7QNP", "", pdbx_doi="10.2210/pdb7np/pdb")["doi"] == "10.2210/pdb7qnp/pdb"
    assert convert(tmp_path, "7QNQ", "", pdbx_doi="10.2210/pdb7qnp/pdb")["doi"] == "10.2210/pdb7qnq/pdb"


def test_doi_of_the_entry_stays_as_given(tmp_path):
    assert convert(tmp_path, "8T5P", "")["doi"] == "10.2210/pdb8t5p/pdb"
    assert convert(tmp_path, "8T5P", "", pdbx_doi="10.2210/pdb_00008t5p/pdb")["doi"] == "10.2210/pdb_00008t5p/pdb"


def test_contact_family_name_with_a_comma_stays_whole(tmp_path):
    body = """loop_
_pdbx_contact_author.id
_pdbx_contact_author.name_first
_pdbx_contact_author.name_last
_pdbx_contact_author.name_mi
_pdbx_contact_author.identifier_ORCID
2 Mark 'Herzik, Jr.' A. 0000-0001-6653-6682
3 Ada  Lovelace     ?  ?
"""
    contacts = [item for item in convert(tmp_path, "11MS", body)["contributors"] if item["contributorType"] == "ContactPerson"]
    assert [(item["name"], item["familyName"], item["givenName"]) for item in contacts] == [
        ("Herzik, Jr., Mark A.", "Herzik, Jr.", "Mark A."),
        ("Lovelace, Ada", "Lovelace", "Ada"),
    ]
    assert contacts[0]["nameIdentifiers"][0]["nameIdentifier"] == "https://orcid.org/0000-0001-6653-6682"
    assert "nameIdentifiers" not in contacts[1]


def test_obsolete_entry_with_more_than_one_successor(tmp_path):
    body = """_pdbx_database_PDB_obs_spr.id OBSLTE
_pdbx_database_PDB_obs_spr.pdb_id '2HHB 3HHB 4HHB'
_pdbx_database_PDB_obs_spr.replace_pdb_id 1HHB
"""
    assert related(convert(tmp_path, "1HHB", body), "IsObsoletedBy") == [
        "10.2210/pdb2hhb/pdb", "10.2210/pdb3hhb/pdb", "10.2210/pdb4hhb/pdb"]


def test_obsolete_entry_without_a_successor(tmp_path):
    body = """_pdbx_database_PDB_obs_spr.id OBSLTE
_pdbx_database_PDB_obs_spr.pdb_id NONE
_pdbx_database_PDB_obs_spr.replace_pdb_id 2HZO
"""
    assert related(convert(tmp_path, "2HZO", body), "IsObsoletedBy") == []


def test_entry_that_replaces_more_than_one_entry(tmp_path):
    body = """_pdbx_database_PDB_obs_spr.id SPRSDE
_pdbx_database_PDB_obs_spr.pdb_id 4HHB
_pdbx_database_PDB_obs_spr.replace_pdb_id '1HHB 1HHC'
"""
    assert related(convert(tmp_path, "4HHB", body), "Obsoletes") == ["10.2210/pdb1hhb/pdb", "10.2210/pdb1hhc/pdb"]
