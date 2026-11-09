"""The wwPDB mmCIF archive: download entry files, mirror the archive into a Hugging Face bucket, read the mirror.

This module does not import the crosswalk, so the mirror job needs only requests and huggingface_hub.
"""

import concurrent.futures
import dataclasses
import datetime
import gzip
import itertools
import json
import logging
import os
import pathlib
import threading
import time

import requests


def log(message):
    logging.getLogger(__name__).info(message)


def archive_layout():
    return {
        "current": {"holdings": "released_structures_last_modified_dates.json.gz", "directory": "divided"},
        "obsolete": {"holdings": "obsolete_structures_last_modified_dates.json.gz", "directory": "obsolete"},
    }


def archive_path(pdb_id, status):
    directory = archive_layout()[status]["directory"]
    return f"data/structures/{directory}/mmCIF/{pdb_id[1:3]}/{pdb_id}.cif.gz"


@dataclasses.dataclass(frozen=True, order=True)
class Entry:
    pdb_id: str
    status: str
    last_modified_ms: int

    @property
    def archive_path(self):
        return archive_path(self.pdb_id, self.status)


def fetch(session, url):
    attempts = 5
    for attempt in range(attempts):
        try:
            response = session.get(url, timeout=60)
            if response.status_code < 500 and response.status_code != 429:
                response.raise_for_status()
                return response.content
            if attempt == attempts - 1:
                response.raise_for_status()
        except requests.RequestException as error:
            permanent = error.response is not None and error.response.status_code < 500 and error.response.status_code != 429
            if permanent or attempt == attempts - 1:
                raise
        time.sleep(2 ** attempt)


def new_session():
    session = requests.Session()
    session.headers["User-Agent"] = "pdb-datacite-crosswalk/0.1"
    return session


def parse_holdings(raw):
    entries = []
    for status, layout in archive_layout().items():
        for pdb_id, stamp in json.loads(gzip.decompress(raw[layout["holdings"]])).items():
            last_modified_ms = round(datetime.datetime.fromisoformat(stamp).timestamp() * 1000)
            entries.append(Entry(pdb_id.lower(), status, last_modified_ms))
    return sorted(entries)


def load_holdings(archive_url):
    raw = {layout["holdings"]: fetch(new_session(), f"{archive_url}holdings/{layout['holdings']}") for layout in archive_layout().values()}
    return raw, parse_holdings(raw)


def snapshot_date(entries):
    newest = max(entry.last_modified_ms for entry in entries)
    return datetime.datetime.fromtimestamp(newest / 1000, datetime.UTC).date().isoformat()


def limit_entries(entries, limit):
    kept = []
    for status in archive_layout():
        kept += [entry for entry in entries if entry.status == status][:limit]
    return sorted(kept)


def list_files(api, bucket, prefix):
    return {item.path: item for item in api.list_bucket_tree(bucket, prefix=prefix, recursive=True) if item.type == "file"}


def needs_download(bucket_file, entry):
    if bucket_file is None or bucket_file.mtime is None:
        return True
    return round(bucket_file.mtime.timestamp() * 1000) != entry.last_modified_ms


def save(path, data, last_modified_ms):
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.write_bytes(data)
    # The bucket takes its mtime from the local file, and the next run compares it with the holdings.
    os.utime(partial, ns=(last_modified_ms * 1_000_000, last_modified_ms * 1_000_000))
    partial.replace(path)


def fetch_ids(ids, out_dir, archive_url, delay):
    out_dir.mkdir(parents=True, exist_ok=True)
    session = new_session()
    for pdb_id in ids:
        pdb_id = pdb_id.lower()
        (out_dir / f"{pdb_id}.cif.gz").write_bytes(fetch_one(session, archive_url, pdb_id))
        time.sleep(delay)


def fetch_one(session, archive_url, pdb_id):
    for status in archive_layout():
        try:
            return fetch(session, archive_url + archive_path(pdb_id, status))
        except requests.HTTPError as error:
            if error.response.status_code != 404:
                raise
    raise LookupError(f"{pdb_id} is not in the current or obsolete archive at {archive_url}")


def mirror(api, bucket, staging_dir, changed, archive_url, threads):
    # One session per thread, so each thread keeps its connection open.
    local = threading.local()

    def download(entry):
        if not hasattr(local, "session"):
            local.session = new_session()
        try:
            data = fetch(local.session, archive_url + entry.archive_path)
        except requests.RequestException as error:
            return f"{type(error).__name__}: {error}"
        save(staging_dir / entry.archive_path, data, entry.last_modified_ms)
        return None

    def upload_batch(added):
        api.batch_bucket_files(bucket, add=added)
        for path, _ in added:
            pathlib.Path(path).unlink()

    failed = {}
    upload = None
    done = 0
    with (concurrent.futures.ThreadPoolExecutor(threads) as downloads,
          concurrent.futures.ThreadPoolExecutor(1) as uploads):
        results = downloads.map(download, changed.values())
        for batch in itertools.batched(zip(changed.items(), results), 1000):
            added = []
            for (key, entry), error in batch:
                if error:
                    failed[entry] = error
                else:
                    added.append((str(staging_dir / entry.archive_path), key))
            # Concurrent bucket batch calls draw HTTP 429, so only one upload runs at a time.
            if upload:
                upload.result()
            upload = uploads.submit(upload_batch, added)
            done += len(batch)
            log(f"mirror: {done}/{len(changed)} fetched from {archive_url}, {len(failed)} failed")
        if upload:
            upload.result()
    return failed


def sync_mirror(api, bucket, prefix, entries, archive_url, staging_dir, threads, prune):
    desired = {f"{prefix}/{entry.archive_path}": entry for entry in entries}
    listing = list_files(api, bucket, f"{prefix}/data/structures/")
    changed = {key: entry for key, entry in desired.items() if needs_download(listing.get(key), entry)}
    log(f"mirror: {len(entries)} entries, {len(changed)} to fetch from {archive_url}")
    failed = mirror(api, bucket, staging_dir, changed, archive_url, threads)
    if prune:
        stale = sorted(listing.keys() - desired.keys())
        if stale:
            api.batch_bucket_files(bucket, delete=stale)
        log(f"mirror: {len(stale)} stale files deleted")
    return failed


def run_mirror(api, bucket, prefix, archive_url, work_dir, limit, threads):
    """Return {entry: error} for the entries that failed to download."""
    holdings, all_entries = load_holdings(archive_url)
    entries = all_entries if limit is None else limit_entries(all_entries, limit)
    # A limited run sees only part of the holdings, so it must not delete the other mirror files.
    failed = sync_mirror(api, bucket, prefix, entries, archive_url, work_dir / "mirror", threads, prune=limit is None)
    # The convert job reads the holdings as its entry list, so they go up after the files they list.
    api.batch_bucket_files(bucket, add=[(raw, f"{prefix}/holdings/{name}") for name, raw in holdings.items()])
    log(f"mirror: holdings written to {prefix}/holdings/")
    return failed


def read_holdings(api, bucket, prefix, holdings_dir):
    listing = list_files(api, bucket, f"{prefix}/holdings/")
    names = [layout["holdings"] for layout in archive_layout().values()]
    keys = {name: f"{prefix}/holdings/{name}" for name in names}
    if not all(key in listing for key in keys.values()):
        raise SystemExit(f"no holdings at {prefix}/holdings/; run jobs/mirror.py first")
    holdings_dir.mkdir(parents=True, exist_ok=True)
    api.download_bucket_files(bucket, [(listing[keys[name]], str(holdings_dir / name)) for name in names])
    return parse_holdings({name: (holdings_dir / name).read_bytes() for name in names})


def list_mirror(api, bucket, prefix):
    return list_files(api, bucket, f"{prefix}/data/structures/")


def mirror_problems(listing, prefix, entries):
    problems = {}
    for entry in entries:
        key = f"{prefix}/{entry.archive_path}"
        bucket_file = listing.get(key)
        if bucket_file is None:
            problems[entry] = f"not in the bucket mirror at {key}"
        elif needs_download(bucket_file, entry):
            held = datetime.datetime.fromtimestamp(entry.last_modified_ms / 1000, datetime.UTC).isoformat()
            mirrored = "no mtime" if bucket_file.mtime is None else f"mtime {bucket_file.mtime.isoformat()}"
            problems[entry] = f"bucket mirror file {key} has {mirrored}, holdings give {held}"
    return problems


def download_mirror(api, bucket, prefix, listing, entries, mirror_dir):
    done = 0
    for chunk in itertools.batched(entries, 1000):
        api.download_bucket_files(bucket, [(listing[f"{prefix}/{entry.archive_path}"], str(mirror_dir / entry.archive_path)) for entry in chunk])
        done += len(chunk)
        log(f"mirror: {done}/{len(entries)} fetched from the bucket")
