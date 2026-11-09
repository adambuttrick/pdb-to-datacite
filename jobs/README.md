# Hugging Face jobs

Two Hugging Face jobs make records for the full wwPDB archive. Each job does one task.

| Job | Task |
|---|---|
| `mirror.py` | Copies the wwPDB mmCIF archive into a Hugging Face bucket. |
| `convert.py` | Makes a record for each entry in the bucket copy, and writes the records to the bucket. |

Run the mirror job first. The convert job does not connect to wwPDB. After a change to the mapping, run only the convert job.

## Run the jobs

Run the commands in the root directory of the repository. Replace `NAMESPACE` with your Hugging Face user or organization and `BUCKET` with the name of your bucket.

```sh
hf jobs uv run --namespace NAMESPACE --flavor cpu-xl --timeout 6h --secrets HF_TOKEN -d \
  -e HF_HUB_DISABLE_PROGRESS_BARS=1 \
  -v ./src:/repo/src \
  jobs/mirror.py --src-dir /repo/src \
  --bucket NAMESPACE/BUCKET --prefix pdb \
  --archive-url https://files.wwpdb.org/pub/pdb/ \
  --work-dir /tmp/pdb --verbose

hf jobs uv run --namespace NAMESPACE --flavor cpu-xl --timeout 6h --secrets HF_TOKEN -d \
  -e HF_HUB_DISABLE_PROGRESS_BARS=1 \
  -v ./src:/repo/src -v ./data:/repo/data -v ./schema:/repo/schema \
  jobs/convert.py --src-dir /repo/src \
  --bucket NAMESPACE/BUCKET --prefix pdb \
  --mapping data/mapping.yaml \
  --lookup funders=/repo/data/funder_ror.csv \
  --xsd /repo/schema/datacite-4.7/metadata.xsd \
  --work-dir /tmp/pdb --workers 16 --verbose
```

The jobs import the `pdb_datacite` package from the `src/` volume.

For a test run, add `--limit N` to the two jobs and use a different `--prefix`. The jobs then use the first N current entries and the first N obsolete entries.

NOTE: Set `--workers` to the CPUs of the flavor (16 on cpu-xl). In a job, `os.cpu_count()` gives the CPUs of the host.

NOTE: Use `--verbose`, not `-v`. `hf jobs uv run` reads `-v` as its volume option, also after the script name.

NOTE: Give the mapping as a local path (`data/mapping.yaml`). `hf jobs uv run` sends each `.yaml` file that an argument names to the job. If the path does not exist on your computer, the command stops with `FileNotFoundError`.

## Mirror job

The mirror job sets the mtime of each bucket file to the wwPDB last-modified time. It downloads a file only if the bucket has no copy or a copy with a different mtime. A full run deletes the bucket files that are not in the wwPDB holdings files. A run with `--limit` does not delete files.

The job writes the holdings files to the bucket last, after the archive files. If a run stops, run the same command again. The next run continues from the bucket contents.

If a download fails, the job writes the PDB ID and the error to stderr. The job then ends with exit status 1.

## Convert job

The convert job reads its list of entries from the holdings files in the bucket. A cache keeps the result for each entry version: the PDB ID, the status and the last-modified time. The job converts only the entries that are not in the cache. The output is the same as a conversion of all entries.

The name of the cache file is a SHA-256 of the files that change the records. These files are the job script, the mapping, the lookup files, the XSD and the files in `src/pdb_datacite/crosswalk/`. If one of these files changes, the next run converts all entries again. A change to other files does not start a new cache.

| Bucket path | Content |
|---|---|
| `<prefix>/holdings/` | The two wwPDB holdings files from the last mirror run. |
| `<prefix>/data/structures/` | The archive files, at the same paths as on files.wwpdb.org. |
| `<prefix>/datacite/<snapshot>/json/` | Shards of DataCite REST API payloads, one record on each line. |
| `<prefix>/datacite/<snapshot>/xml/` | Shards of DataCite XML records, one `{"doi", "xml"}` object on each line. |
| `<prefix>/datacite/<snapshot>/incomplete/` | The incomplete records in the same two formats, and `reasons.jsonl`. The job does not validate these records against the XSD. |
| `<prefix>/datacite/<snapshot>/errors.jsonl` | One line for each entry error, with `pdb_id`, `stage` and `message`. |
| `<prefix>/datacite/<snapshot>/MANIFEST.json` | Counts, the list of shards and the SHA-256 of each input file. |
| `<prefix>/datacite/cache/` | The conversion cache. |

`<snapshot>` is the date of the most recent last-modified time in the holdings files. A run with `--limit N` writes to `<snapshot>-limitN/`. A run replaces all files in its directory.

| Error stage | Meaning |
|---|---|
| `download` | The bucket has no copy, or an old copy, of the archive file of an entry to convert. The entry has no record. Run the mirror job, then the convert job. |
| `convert` | The converter failed for the entry. The entry has no record. |
| `xsd` | The XML record is not valid for the XSD. The record is in the output. |
| `duplicate` | An entry with a lower PDB ID has the same DOI. The entry has no record. |
