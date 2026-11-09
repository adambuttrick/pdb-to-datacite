# PDB to DataCite crosswalk

This document describes how pdb-datacite maps a PDB entry file (PDBx/mmCIF) to a DataCite Metadata Schema 4.7 record. Each entry gives one record for its PDB DOI, `10.2210/pdbXXXX/pdb`.

`data/mapping.yaml` holds the same mapping in the form that the converter reads. The [README](README.md) tells how to run the converter and how to change the mapping.

## Principles

1. The record uses only data from the entry file. It does not use data from external sources such as OpenAlex, ORCID, PubMed or the publisher.
2. One PDB value gives one DataCite value. The converter does not make text from many fields.
3. Values stay as the depositor or the wwPDB gave them. The converter changes only whitespace, DOI prefixes and the letter case of UniProt accessions.
4. Fixed lookups add identifiers to PDB values. The table below gives the lookups.
5. The crosswalk uses schema 4.7. Version 4.7 adds the relation type `Other` with `relationTypeInformation`. With this relation type, the record keeps the PDB labels of related entries.

| Lookup | Input | Output |
|---|---|---|
| Funder table (`data/funder_ror.csv`) | `_pdbx_audit_support.funding_organization` | ROR ID |
| Resolver patterns | PDB, EMDB, BMRB, SASBDB and UniProt accessions, EC numbers | DOI or URL |
| NCBI Taxonomy | NCBI taxonomy ID | Taxonomy browser URI |

## Mandatory and optional properties

The mapping has two blocks of properties. The `mandatory` block has the six DataCite mandatory properties and `url`. Each of these properties must have a value. The `optional` block has all other properties. If an optional property has no value, the record does not include it.

If an entry has no value for a mandatory property, the converter does not write its record to the output. It writes the incomplete record to a different directory, with the names of the missing properties. For example, some obsolete entries have no title.

## Property map

The table gives the properties in the order of the record.

| DataCite property | PDBx/mmCIF source | Rule |
|---|---|---|
| Identifier (DOI) | `_database_2.pdbx_DOI` (row with `database_id` PDB) | Use the DOI as is if it is the DOI of the entry. If the value is not a valid DOI, or if it is the DOI of a different entry, make the DOI from the entry ID. For example, the file of 7QNP gives `10.2210/pdb7np/pdb`. |
| url | `_database_2.pdbx_database_accession` | `https://www.wwpdb.org/pdb?id=pdb_0000xxxx`, with the extended PDB ID in small letters. |
| ResourceType | constant | `Dataset`, with the resource type "Structure model". "Structure model" is the PDB value of `data_content_type`. |
| Creator | `_audit_author.name`, `.identifier_ORCID` | Order by `pdbx_ordinal`. Split "Family, Given" at the first comma. A name without a comma gets no `nameType`. Add the ORCID iD as a name identifier. |
| Title | `_struct.title` | Use as is. |
| Publisher | constant | Worldwide Protein Data Bank, with the ROR ID `047qy5748`. |
| PublicationYear | `_pdbx_audit_revision_history`, `_pdbx_database_status.recvd_initial_deposition_date` | Year of the first "Structure model" revision. If that revision has no date, use the year of the deposition date. |
| Subject | `_struct_keywords.pdbx_keywords`, `.text` | One subject for `pdbx_keywords` and one subject for each part of `text`. Commas separate the parts. |
| Subject | `_entity_src_gen`, `_entity_src_nat`, `_pdbx_entity_src_syn`, `_em_entity_assembly_naturalsource` | One subject for each source organism, with the scheme "NCBI Taxonomy", a URI and the taxonomy ID as `classificationCode`. The record does not include expression hosts. |
| Subject | `_exptl.method` | One subject for each experimental method. The scheme is "PDBx/mmCIF _exptl.method". The scheme URI is the dictionary page of the item. |
| Subject | `_entity.pdbx_description` (polymer entities) | One subject for each molecule name, without a scheme. |
| Subject | `_entity.pdbx_ec` | One subject for each EC number. See [UniProt and EC numbers](#uniprot-and-ec-numbers). |
| Contributor (ContactPerson) | `_pdbx_contact_author` | Name and ORCID iD. The family name is `name_last`. The given name is `name_first` and `name_mi`. The record does not include the email address. |
| Contributor (DataCurator) | `_pdbx_database_status.process_site` | The name of the wwPDB site for the site code: RCSB, PDBE, PDBJ, PDBC, BMRB, BNL or NDB. |
| Contributor (ResearchGroup) | `_pdbx_SG_project.full_name_of_center` | Structural genomics centers only. |
| Date (Submitted) | `_pdbx_database_status.recvd_initial_deposition_date` | Deposition date. |
| Date (Available) | first revision in `_pdbx_audit_revision_history` | Release date. See [Date information](#date-information). |
| Date (Updated) | later revisions | One date for each revision. |
| Date (Withdrawn) | revision with the type "Obsolete" | Obsolete entries only. |
| Date (Collected) | `_diffrn_detector.pdbx_collection_date`, `_em_imaging.date` | Full `YYYY-MM-DD` values only. |
| Language | constant | `en`. |
| AlternateIdentifier | `_entry.id`, extended PDB ID | Types "PDB ID" and "Extended PDB ID". |
| RelatedIdentifier | `_citation` (id `primary`) | DOI and PubMed ID, `IsSupplementTo`. |
| RelatedIdentifier | `_citation` (other ids) | DOI and PubMed ID, `References`. |
| RelatedIdentifier | `_pdbx_database_related` | `Other`, with the PDB label in `relationTypeInformation`. See [Related entries](#related-entries). |
| RelatedIdentifier | `_pdbx_initial_refinement_model` (source PDB) | DOI of the starting model, `IsDerivedFrom`. |
| RelatedIdentifier | `_pdbx_related_exp_data_set.data_reference` | DOI of the raw data, `IsDerivedFrom`. |
| RelatedIdentifier | `_pdbx_database_PDB_obs_spr` | `SPRSDE` gives `Obsoletes`. `OBSLTE` gives `IsObsoletedBy`. Each PDB ID in the row gives one relation. For example, 2HHB, 3HHB and 4HHB replace 1HHB. |
| RelatedIdentifier | `_struct_ref.pdbx_db_accession` (`db_name` UNP) | UniProt URL, `References`. See [UniProt and EC numbers](#uniprot-and-ec-numbers). |
| RelatedItem | `_citation`, `_citation_author` | A citation without a DOI or PubMed ID gives a full reference: title, authors, year, volume, issue and pages. The record does not include "To be published" citations. |
| RelatedItem | `_pdbx_deposit_group` | `IsPartOf` a `Collection`. The title is `group_title`. The `number` is the group ID. |
| RelatedItem | `_software`, `_em_software`, `_pdbx_nmr_software` | `IsCompiledBy` a `Software` item. The title is `name`, and `edition` is `version`. One item for each name and version. |
| RelatedItem | `_em_imaging.microscope_model` | `IsCollectedBy` an `Instrument` item. The title is the model name. |
| Format | `_pdbx_database_status.pdb_format_compatible` | PDBx/mmCIF and PDBML. Add PDB if the value is `Y`. |
| Version | last "Structure model" revision | `major.minor`. |
| Rights | constant | CC0 1.0. This is the wwPDB usage policy. |
| Description (Methods) | `_exptl_crystal_grow.pdbx_details` | One description for each different text. The text is as the depositor gave it. |
| FundingReference | `_pdbx_audit_support` | `funderName` is `funding_organization` as given. `awardNumber` is `grant_number` as given. The ROR ID comes from the funder table. |

The record includes each keyword and molecule name one time only. This check ignores the letter case.

## Date information

Each revision date (Available, Updated and Withdrawn) gets a `dateInformation` value from one PDB field:

1. The `_pdbx_audit_revision_group.group` values of the revision, with commas between them. An example is "Database references, Structure summary".
2. If the revision has no groups, `_pdbx_audit_revision_details.type`. An example is "Initial release".

The Submitted and Collected dates have no `dateInformation`. The Version property gives the version number.

A revision without a date gives no date in the record. Some obsolete entries have no date for the initial release. These records have no Available date, and their PublicationYear is the year of the deposition date.

## Related entries

`_pdbx_database_related` links an entry to entries in other archives. The PDB label of the link (`content_type`) goes into `relationTypeInformation`. The record does not include the free-text `details`.

The labels "unspecified" and "other" do not tell what the relation is. The record does not include links with these labels.

| `db_name` | Identifier | Type |
|---|---|---|
| PDB | `10.2210/pdbxxxx/pdb` | DOI |
| EMDB | `https://www.ebi.ac.uk/emdb/EMD-NNNNN` | URL |
| BMRB | `https://bmrb.io/data_library/summary/index.php?bmrbId=NNNNN` | URL |
| SASBDB | `https://identifiers.org/sasbdb:SASXXXX` | URL |

The record does not include links to other databases, for example TargetDB, NDB and PDB-Dev. EMDB entries have no DOIs. Older BMRB entries have no DataCite DOIs. For this reason, all BMRB links are URLs. SASBDB links use the identifiers.org resolver.

## UniProt and EC numbers

`_struct_ref` gives the reference sequence of each polymer. The record uses only the rows with `db_name` UNP. It does not use rows for other databases, for example GenBank, PIR and EMBL.

| Value | Rule | Identifier |
|---|---|---|
| UniProt accession | The converter changes the accession to capital letters. An isoform suffix such as `-2` stays. If the accession does not agree with the UniProt format, the record does not include it. | `https://www.uniprot.org/uniprotkb/P69905`, type URL, relation `References` |
| EC number | The converter splits the value at commas. Partial numbers such as `3.4.21.-` stay. If a value does not agree with the EC format, the record does not include it. | Subject scheme "Enzyme Commission number". `valueURI` is `https://www.enzyme-database.org/query.php?ec=3.4.21.4`. `classificationCode` is the EC number. |

The EC URI goes to ExplorEnz, the IUBMB enzyme list. ExplorEnz also shows partial numbers.

## Funder table

`data/funder_ror.csv` maps each PDB funder string to a ROR ID. The `pdb-datacite funder-table` command makes the table with these rules, in order:

1. Generic values, for example "Not funded" and "Other government", are not funders. The record does not include them.
2. A name in the PDBx dictionary list maps to a ROR record if all of these conditions are true:
   - After normalization, the name is exactly equal to a name, label or alias of the ROR record. For "Parent/Child" names, the rule uses the child part.
   - The ROR record is in the country that the dictionary gives for the name.
   - Only one ROR record agrees.
3. A free-text name gets the mapping of a dictionary name if all of these conditions are true:
   - The two names have the same base name. The base name is the text before the parentheses.
   - The text in the free-text parentheses adds nothing to the text in the dictionary parentheses.
   - The free-text name repeats any country that the dictionary name gives.
   - Exactly one dictionary name agrees.
4. Other free-text names use rule 2 only if they give a country in parentheses. An example is "Medical Research Council (United Kingdom)".
5. `data/funder_ror_decisions.csv` overrides rules 2 to 4. It holds a reviewed decision for each dictionary name that the rules do not map.

A note in each decision gives its reason. Some notes start with "interpretive". These decisions use evidence outside the name, for example the format of grant numbers. To keep only the mappings from names, remove these rows.

A free-text name without a country gets no mapping, for example "National Institutes of Health". Without a country, the name can refer to more than one funder.

The [README](README.md) gives the commands that make the table and the meaning of each status.

## Data not in the record

| Data | Reason |
|---|---|
| Creator affiliations | PDB entry files do not contain author affiliations. |
| Abstract | PDB entries do not have an abstract. `_citation.abstract` is the abstract of the article. |
| Technical description | PDB has no single field for this description. Text from many fields is not a one-to-one mapping. |
| Related entries with the label "unspecified" or "other" | The label does not tell what the relation is. |
| Resolution, R factors, space group | DataCite has no fields for these values. |
| Contact author email | DataCite has no email field, and the record is public. |
| Funder country | DataCite `fundingReference` has no country field. |
| Software identifiers | PDB gives only the names and versions of software. |
| Synchrotron site and beamline | The title of an instrument item needs two fields, the site and the beamline. This is not a one-to-one mapping. |
| Instrument identifiers | PDB gives facility, beamline and microscope names, not instrument identifiers. |
| Initial models that are not PDB entries | The accession formats are not consistent, for example `AF-Q2FXG9-F1` and `Q9UHN6`. |
| File sizes | The entry file does not give file sizes. |
