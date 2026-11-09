# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "huggingface_hub>=2.2,<3",
#     "requests>=2.32",
# ]
# ///
"""Copy the wwPDB mmCIF archive into a Hugging Face bucket, at the wwPDB paths under the prefix.

A bucket file's mtime is the wwPDB last-modified time from the holdings files, so a run downloads
only new and changed files. A full run deletes the files that are not in the holdings. The
holdings files go to <prefix>/holdings/ last, so they never list an entry version that a
completed run did not try to copy. A run that stops leaves a state that the next run continues
from. A failed download goes to stderr and gives exit status 1.
"""

import argparse
import logging
import pathlib
import sys

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


def url_root(value):
    return value if value.endswith("/") else value + "/"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--src-dir", required=True, type=pathlib.Path, help="directory that holds the pdb_datacite package")
    parser.add_argument("--bucket", required=True, help="bucket ID, NAMESPACE/BUCKET")
    parser.add_argument("--prefix", required=True, type=bucket_prefix, help="key prefix in the bucket, for example pdb")
    parser.add_argument("--archive-url", required=True, type=url_root, help="wwPDB archive root, for example https://files.wwpdb.org/pub/pdb/")
    parser.add_argument("--work-dir", required=True, type=pathlib.Path, help="local directory for files waiting to upload")
    parser.add_argument("--limit", type=positive_int, help="copy only the first N current and first N obsolete entries")
    parser.add_argument("--download-threads", type=positive_int, default=128)
    # Not -v: `hf jobs uv run` reads -v as its volume option, also after the script name.
    parser.add_argument("--verbose", action="store_true", help="write progress to stderr")
    return parser.parse_args()


def main():
    args = parse_args()
    sys.path.insert(0, str(args.src_dir))
    from pdb_datacite import archive
    logging.basicConfig(format="%(message)s", stream=sys.stderr)
    logging.getLogger(archive.__name__).setLevel(logging.INFO if args.verbose else logging.WARNING)
    disable_progress_bars()
    failed = archive.run_mirror(HfApi(), args.bucket, args.prefix, args.archive_url, args.work_dir,
                                args.limit, args.download_threads)
    for entry, error in sorted(failed.items()):
        print(f"{entry.pdb_id}: {error}", file=sys.stderr)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
