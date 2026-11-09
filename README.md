# pdb-datacite

pdb-datacite makes DataCite Metadata Schema 4.7 records from PDB entry files (PDBx/mmCIF). Each entry gives one record for its PDB DOI, `10.2210/pdbXXXX/pdb`. The output is a DataCite REST API payload (JSON), DataCite XML, or both.

## How it works

A YAML mapping controls the conversion. For each DataCite property, the mapping names the PDB data that gives the value. The converter reads one entry file, applies the mapping and makes one record.

The mapping in `data/mapping.yaml` obeys these rules:

1. The record uses only data from the entry file. It does not get data from external sources such as OpenAlex, ORCID or PubMed.
2. One PDB value gives one DataCite value. The converter does not make text from many fields.
3. Values stay as the depositor or the wwPDB gave them. The converter changes only whitespace, DOI prefixes and the letter case of UniProt accessions.
4. Fixed lookups add identifiers to PDB values. The lookups are a reviewed funder to ROR table, resolver patterns for identifiers and accession numbers, and NCBI Taxonomy URIs.

| DataCite property | PDB source |
|---|---|
| `doi` | `_database_2.pdbx_DOI`, or a DOI from the entry ID if that item has no valid DOI |
| `url` | `https://www.wwpdb.org/pdb?id=` and the extended PDB ID |
| `types` | Constant: `Dataset`, "Structure model" |
| `creators` | `_audit_author`, with ORCID iDs |
| `titles` | `_struct.title` |
| `publisher` | Constant: Worldwide Protein Data Bank, with its ROR ID |
| `publicationYear` | Year of the first revision, or year of deposition if that revision has no date |
| `subjects` | Keywords, source organisms (NCBI Taxonomy), experimental methods, polymer names, EC numbers |
| `contributors` | Contact authors, the wwPDB site that processed the entry, structural genomics centers |
| `dates` | Deposition, release, revisions, obsoletion, data collection |
| `language` | Constant: `en` |
| `alternateIdentifiers` | PDB ID and extended PDB ID |
| `relatedIdentifiers` | Citation DOIs and PubMed IDs, related entries (PDB, EMDB, BMRB, SASBDB), starting models, source data sets, replaced and replacing entries, UniProt accessions |
| `relatedItems` | Citations, deposit groups, software, electron microscopes |
| `formats` | PDBx/mmCIF and PDBML, and PDB if the entry is compatible with that format |
| `version` | Version of the last revision |
| `rightsList` | Constant: CC0 1.0 |
| `descriptions` | Crystal growth details, as a `Methods` description |
| `fundingReferences` | `_pdbx_audit_support`, with ROR IDs from the funder table |
| `schemaVersion` | Constant: `http://datacite.org/schema/kernel-4` |

[CROSSWALK.md](CROSSWALK.md) gives the rules for each property and the data that the record does not include.

### Mandatory properties

The mapping has two blocks of properties. Each property in the `mandatory` block must have a value. The block has the six DataCite mandatory properties and `url`. The DataCite REST API needs `url` to register a DOI. If a property in the `optional` block has no value, the record does not include it.

If an entry has no value for a mandatory property, its record is incomplete. The converter writes incomplete records to a different directory. The file `reasons.jsonl` in that directory gives the missing properties of each record:

```json
{"input": "mmcif/2ldh.cif.gz", "files": ["pdb2ldh.json", "pdb2ldh.xml"], "missing": ["titles"]}
```

## Install

Python 3.12 or later is necessary. In the root directory of the repository, run:

```sh
pip install .
```

This command installs the `pdb_datacite` package and the `pdb-datacite` command.

The package does not include its data files. Give their paths to the commands:

| Path | Content |
|---|---|
| `data/mapping.yaml` | The mapping. |
| `data/funder_ror.csv` | The funder to ROR table. It has one row for each PDB funder string. |
| `data/funder_ror_decisions.csv` | Reviewed decisions for the funder names that the rules do not map. |
| `schema/datacite-4.7/` | The DataCite 4.7 XSD. |

## Commands

| Command | Task |
|---|---|
| `fetch` | Downloads entry files from a wwPDB archive. |
| `convert` | Makes DataCite records with the mapping. |
| `validate` | Validates DataCite XML records against the XSD. |
| `funder-usage` | Counts the PDB funding rows for each funder string. |
| `funder-table` | Makes the funder to ROR table. |

`pdb-datacite COMMAND --help` shows the options of a command.

### Make records

```sh
pdb-datacite fetch 8T5P 6VXX -o mmcif --archive-url https://files.wwpdb.org/pub/pdb/
pdb-datacite convert mmcif/*.cif.gz --mapping data/mapping.yaml --lookup funders=data/funder_ror.csv \
  -o out/records --incomplete-dir out/incomplete --format json xml
pdb-datacite validate out/records/*.xml --xsd schema/datacite-4.7/metadata.xsd
```

`fetch` gets each entry from the current archive. If the entry is not there, `fetch` gets it from the obsolete archive. It saves the archive file unchanged.

`convert` reads `.cif` and `.cif.gz` files. It writes one file for each record and format, with the DOI suffix as the name (`pdb8t5p.json`). If the converter cannot read an entry, it writes the file name and the error to stderr.

`validate` writes the first five errors of each file to stderr. If a file is not valid, the exit status is 1.

### Make the funder table

`data/funder_ror.csv` maps each PDB funder string (`_pdbx_audit_support.funding_organization`) to a ROR ID. The rules are conservative. The docstring of `pdb_datacite.funders` gives the rules.

| Status | Meaning |
|---|---|
| `exact` | Dictionary name, exact ROR name in the same country. |
| `reviewed` | Dictionary name, mapped by a reviewed decision. |
| `reviewed_none` | Dictionary name, reviewed, no ROR record. |
| `variant` | Free text that gets the mapping of a dictionary name. |
| `variant_unmapped` | Free text that agrees with a dictionary name that has no mapping. |
| `legacy_exact` | Free text with a country, exact ROR name in that country. |
| `legacy_unmapped` | Free text without a mapping. |
| `excluded` | Generic value, not a funder. |
| `review` | Dictionary name that needs a decision. |

To make the table for a new ROR release or PDBx/mmCIF dictionary, run:

```sh
mkdir -p out
curl -o out/mmcif_pdbx_v50.dic.gz https://mmcif.wwpdb.org/dictionaries/ascii/mmcif_pdbx_v50.dic.gz
pdb-datacite funder-usage -o out/pdbj_funder_usage.csv
pdb-datacite funder-table --dictionary out/mmcif_pdbx_v50.dic.gz --ror-dump path/to/ror-data.json \
  --usage out/pdbj_funder_usage.csv --decisions data/funder_ror_decisions.csv -o data/funder_ror.csv
```

`funder-usage` counts the funding rows for each funder string with the PDBj Mine SQL service. Find the rows with status `review` in the new table. Add a decision for each row to `data/funder_ror_decisions.csv`, and make the table again.

NOTE: The table in this repository uses ROR v2.15 (2026-10-20) and the PDBx/mmCIF dictionary 5.418.

## Python API

```python
from pathlib import Path

from pdb_datacite import crosswalk

walk = crosswalk.load(Path("data/mapping.yaml"), {"funders": Path("data/funder_ror.csv")})
conversion = walk.convert(Path("mmcif/8t5p.cif.gz"))
if not conversion.missing:
    xml = crosswalk.to_xml(conversion.record)
```

`load` reads and checks the mapping. If the mapping has an error, `load` raises `MappingError` with the YAML path of the error.

`convert` gives a `Conversion` for one entry file. `record` is the `attributes` object of a DataCite REST API payload. `missing` gives the mandatory properties that have no value. The record is complete only if `missing` is empty.

`to_xml` gives the DataCite XML of a record.

## Change the mapping

Each property in `data/mapping.yaml` has a list of `sources`. A source is a builder with its parameters. A list property gets the values of all its sources. A single-value property gets the value of the first source that gives a value. `distinct` removes repeated values across the sources of a property. The record has the properties in the order of the file.

To add a one-to-one field, use the `field` builder. `field` reads one PDB item and gives one value for each row. `{value}` in `emit` is that value. This example adds the NMR spectrometer of an entry as an Instrument related item:

```yaml
  relatedItems:
    sources:
      # ... the sources that are there now
      - field:
          item: pdbx_nmr_spectrometer.model
          distinct: true
          emit: {relationType: IsCollectedBy, relatedItemType: Instrument, titles: [{title: '{value}'}]}
```

The docstring of `field` gives its other parameters: `where`, `order_by`, `split`, `clean`, `case`, `resolver` and `first`.

The builders are the public functions in `pdb_datacite.crosswalk.builders`. The name of the function is the builder name in the YAML. For a rule that `field` cannot do, add a public function to that module. You do not have to register the function.

`lookups` names each CSV table that the mapping reads, with its key column. Give the file for each lookup with `--lookup NAME=PATH`.

The converter checks the mapping before it converts an entry. If the mapping has an error, the converter stops and gives the YAML path of the error.
