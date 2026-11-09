# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "gemmi==0.7.5",
#     "huggingface_hub>=2.2,<3",
#     "pyyaml==6.0.3",
#     "requests>=2.32",
#     "xmlschema==4.3.2",
# ]
# ///
"""Make DataCite records for every entry of the bucket mirror of the wwPDB mmCIF archive.

Run jobs/mirror.py first: the entry list is the holdings files in the bucket, and the archive
files come from the bucket mirror. An entry whose mirror file is missing or out of date gets a
`download` error. A cache in the bucket keeps the result of each entry version, so a run
converts only new and changed entries. A change to this script, the mapping, a lookup file, the
converter package or the XSD starts a new cache. An entry without a value for a mandatory
property gets its record in incomplete/, and incomplete/reasons.jsonl names the missing properties.
"""

import argparse
import collections
import concurrent.futures
import dataclasses
import functools
import gzip
import hashlib
import itertools
import json
import logging
import multiprocessing
import pathlib
import sys

import xmlschema
from huggingface_hub import HfApi
from huggingface_hub.utils import disable_progress_bars


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def bucket_prefix(value):
    value = value.strip("/")
    if not value:
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def lookup(value):
    name, separator, path = value.partition("=")
    if not separator or not name or not path:
        raise argparse.ArgumentTypeError("must be NAME=PATH")
    return name, pathlib.Path(path)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bucket", required=True, help="bucket ID, NAMESPACE/BUCKET")
    parser.add_argument("--prefix", required=True, type=bucket_prefix, help="key prefix in the bucket, for example pdb")
    parser.add_argument("--src-dir", required=True, type=pathlib.Path, help="directory that holds the pdb_datacite package")
    parser.add_argument("--mapping", required=True, type=pathlib.Path, help="data/mapping.yaml")
    parser.add_argument("--lookup", action="append", default=[], type=lookup, metavar="NAME=PATH",
                        help="a lookup file that the mapping names, for example funders=data/funder_ror.csv (repeatable)")
    parser.add_argument("--xsd", required=True, type=pathlib.Path, help="DataCite 4.7 metadata.xsd")
    parser.add_argument("--work-dir", required=True, type=pathlib.Path, help="local directory for the archive files and outputs")
    # Required: in a job, os.cpu_count() gives the CPUs of the host, not of the job.
    parser.add_argument("--workers", required=True, type=positive_int)
    parser.add_argument("--limit", type=positive_int, help="use only the first N current and first N obsolete entries")
    parser.add_argument("--shard-size", type=positive_int, default=10000, help="records in each output file")
    # Not -v: `hf jobs uv run` reads -v as its volume option, also after the script name.
    parser.add_argument("--verbose", action="store_true", help="write progress to stderr")
    args = parser.parse_args()
    names = [name for name, _ in args.lookup]
    if len(set(names)) < len(names):
        parser.error("--lookup: each NAME may be given once")
    return args


def log(message):
    logging.getLogger(__name__).info(message)


def list_files(api, bucket, prefix):
    return {item.path: item for item in api.list_bucket_tree(bucket, prefix=prefix, recursive=True) if item.type == "file"}


@functools.cache
def load_crosswalk(src_dir, mapping, lookups, xsd):
    sys.path.insert(0, str(src_dir))
    from pdb_datacite import crosswalk
    return crosswalk, crosswalk.load(mapping, dict(lookups)), xmlschema.XMLSchema(str(xsd))


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclasses.dataclass(frozen=True)
class Converted:
    pdb_id: str
    doi: str | None
    json_line: str | None
    xml_line: str | None
    missing: tuple
    errors: tuple


def convert_entry(sources, task):
    pdb_id, path = task
    package, walk, schema = load_crosswalk(*sources)
    try:
        conversion = walk.convert(path)
        xml = package.to_xml(conversion.record)
    except Exception as error:  # noqa: BLE001
        return Converted(pdb_id, None, None, None, (), (("convert", f"{type(error).__name__}: {error}"),))
    record, missing = conversion.record, conversion.missing
    # Incomplete records are not published, so they skip the XSD check.
    invalid = [] if missing else list(schema.iter_errors(xml))
    errors = (("xsd", f"{invalid[0].reason} at {invalid[0].path} ({len(invalid)} errors)"),) if invalid else ()
    payload = {"data": {"type": "dois", "attributes": record}}
    doi = record.get("doi")
    return Converted(pdb_id, doi, compact(payload), compact({"doi": doi, "xml": xml}), missing, errors)


def convert(sources, tasks, workers):
    # Spawn, not fork: hf_xet threads run in this process by now, and a forked child of a threaded process can deadlock.
    spawn = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(workers, spawn, load_crosswalk, sources) as pool:
        yield from pool.map(functools.partial(convert_entry, sources), tasks, chunksize=64)


def cached_or_converted(entries, cache, failed, converted):
    for entry in entries:
        if entry in failed:
            continue
        if entry not in cache:
            cache[entry] = next(converted)
        yield cache[entry]


def load_cache(api, bucket, cache_dir, path, archive):
    bucket_file = list_files(api, bucket, cache_dir).get(cache_dir + path.name)
    if bucket_file is None:
        log(f"cache: {cache_dir + path.name} not found, all entries will be converted")
        return {}
    api.download_bucket_files(bucket, [(bucket_file, str(path))])
    cache = {}
    with gzip.open(path) as handle:
        for line in handle:
            item = json.loads(line)
            cache[archive.Entry(item["pdb_id"], item["status"], item["last_modified_ms"])] = Converted(
                item["pdb_id"], item["doi"], item["json_line"], item["xml_line"], tuple(item["missing"]),
                tuple(map(tuple, item["errors"])))
    log(f"cache: {len(cache)} entries in {cache_dir + path.name}")
    return cache


def write_cache(path, cache, entries):
    write_gzip_lines(path, (compact(dataclasses.asdict(entry) | dataclasses.asdict(cache[entry])) for entry in entries if entry in cache))


def accepted_records(results, errors, incomplete):
    owners = {}
    for result in results:
        errors += [{"pdb_id": result.pdb_id, "stage": stage, "message": message} for stage, message in result.errors]
        if result.json_line is None:
            continue
        if result.missing:
            incomplete.append(result)
            continue
        if result.doi in owners:
            errors.append({"pdb_id": result.pdb_id, "stage": "duplicate",
                           "message": f"DOI {result.doi} is also the DOI of {owners[result.doi]}"})
            continue
        owners[result.doi] = result.pdb_id
        yield result.json_line, result.xml_line


def write_gzip_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(path, "wb", mtime=0) as handle:
        for line in lines:
            handle.write(line.encode("utf-8") + b"\n")


def write_shards(records, out_dir, prefix, shard_size):
    shards = []
    for index, batch in enumerate(itertools.batched(records, shard_size)):
        name = f"part-{index:05d}.jsonl.gz"
        shard = {"json": f"{prefix}json/{name}", "xml": f"{prefix}xml/{name}", "records": len(batch)}
        write_gzip_lines(out_dir / shard["json"], (json_line for json_line, _ in batch))
        write_gzip_lines(out_dir / shard["xml"], (xml_line for _, xml_line in batch))
        shards.append(shard)
        log(f"convert: {prefix}{name} written with {len(batch)} records")
    return shards


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def input_digests(src_dir, paths):
    # Names relative to the source directory, so crosswalk/__init__.py and __init__.py do not collide.
    root = src_dir.resolve()
    digests = {}
    for path in paths:
        resolved = path.resolve()
        name = str(resolved.relative_to(root)) if resolved.is_relative_to(root) else path.name
        if name in digests:
            raise SystemExit(f"two input files have the name {name} in MANIFEST.json")
        digests[name] = sha256(path)
    return digests


def cache_name(digests):
    return hashlib.sha256("".join(digests.values()).encode()).hexdigest() + ".jsonl.gz"


def write_outputs(args, entries, snapshot, failed, results, digests, converted, statuses):
    out_dir = args.work_dir / "out"
    errors = [{"pdb_id": entry.pdb_id, "stage": "download", "message": message} for entry, message in failed.items()]
    incomplete = []
    shards = write_shards(accepted_records(results, errors, incomplete), out_dir, "", args.shard_size)
    errors.sort(key=lambda error: (error["pdb_id"], error["stage"], error["message"]))
    (out_dir / "errors.jsonl").write_text("".join(compact(error) + "\n" for error in errors), encoding="utf-8")
    incomplete_shards = write_shards(((result.json_line, result.xml_line) for result in incomplete),
                                     out_dir, "incomplete/", args.shard_size)
    (out_dir / "incomplete").mkdir(parents=True, exist_ok=True)
    (out_dir / "incomplete" / "reasons.jsonl").write_text("".join(
        compact({"pdb_id": result.pdb_id, "doi": result.doi, "missing": list(result.missing)}) + "\n"
        for result in incomplete), encoding="utf-8")
    manifest = {
        "snapshot": snapshot,
        "limit": args.limit,
        "entries": {status: sum(entry.status == status for entry in entries) for status in statuses},
        "converted": converted,
        "reused": len(entries) - len(failed) - converted,
        "records": sum(shard["records"] for shard in shards),
        "errors": dict(sorted(collections.Counter(error["stage"] for error in errors).items())),
        "shards": shards,
        "incomplete": {
            "records": len(incomplete),
            "missing": dict(sorted(collections.Counter(name for result in incomplete for name in result.missing).items())),
            "shards": incomplete_shards,
        },
        "sha256": digests,
    }
    (out_dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"convert: {manifest['records']} records, {len(incomplete)} incomplete, errors by stage {manifest['errors']}")
    files = [name for shard in shards + incomplete_shards for name in (shard["json"], shard["xml"])]
    return files + ["incomplete/reasons.jsonl", "errors.jsonl", "MANIFEST.json"]


def publish(api, bucket, destination, out_dir, names):
    api.batch_bucket_files(bucket, add=[(str(out_dir / name), destination + name) for name in names])
    stale = sorted(set(list_files(api, bucket, destination)) - {destination + name for name in names})
    if stale:
        api.batch_bucket_files(bucket, delete=stale)
    log(f"publish: {len(names)} files to {destination}, {len(stale)} old files deleted")


def main():
    args = parse_args()
    logging.basicConfig(format="%(message)s", stream=sys.stderr)
    disable_progress_bars()
    sources = (args.src_dir, args.mapping, tuple(args.lookup), args.xsd)
    _, walk, _ = load_crosswalk(*sources)
    from pdb_datacite import archive
    for logger in (__name__, archive.__name__):
        logging.getLogger(logger).setLevel(logging.INFO if args.verbose else logging.WARNING)
    # This script is a cache input for its pinned versions and record code. archive.py is not: each cache key holds the entry version.
    digests = input_digests(args.src_dir, (pathlib.Path(__file__), *walk.input_files, args.xsd))
    api = HfApi()
    all_entries = archive.read_holdings(api, args.bucket, args.prefix, args.work_dir / "holdings")
    snapshot = archive.snapshot_date(all_entries)
    entries = all_entries if args.limit is None else archive.limit_entries(all_entries, args.limit)
    cache_dir = f"{args.prefix}/datacite/cache/"
    cache_path = args.work_dir / "cache" / cache_name(digests)
    cache = load_cache(api, args.bucket, cache_dir, cache_path, archive)
    todo = [entry for entry in entries if entry not in cache]
    log(f"cache: {len(entries) - len(todo)} of {len(entries)} entries cached, {len(todo)} to convert")
    listing = archive.list_mirror(api, args.bucket, args.prefix)
    failed = archive.mirror_problems(listing, args.prefix, todo)
    ready = [entry for entry in todo if entry not in failed]
    archive.download_mirror(api, args.bucket, args.prefix, listing, ready, args.work_dir / "mirror")
    tasks = [(entry.pdb_id, args.work_dir / "mirror" / entry.archive_path) for entry in ready]
    results = cached_or_converted(entries, cache, failed, convert(sources, tasks, args.workers))
    names = write_outputs(args, entries, snapshot, failed, results, digests, len(tasks), archive.archive_layout())
    run = snapshot if args.limit is None else f"{snapshot}-limit{args.limit}"
    publish(api, args.bucket, f"{args.prefix}/datacite/{run}/", args.work_dir / "out", names)
    # A limited run keeps the cached results of the entries outside its limit.
    write_cache(cache_path, cache, all_entries)
    publish(api, args.bucket, cache_dir, cache_path.parent, [cache_path.name])


if __name__ == "__main__":
    main()
