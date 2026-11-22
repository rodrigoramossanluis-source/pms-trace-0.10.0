#!/usr/bin/env python3
# SPDX-License-Identifier: MIT  -  Copyright (c) 2026 Rodrigo Ramos
"""s3grep.py - reference secondary-archive search for pms_trace.sh (legacy / "legacy-stack" logs).

Like s3search.py it only structures a search; nothing is indexed or modified.

  s3grep.py find <literal> <bucket-glob> <YYYY-MM-DD>   raw lines containing <literal>
  exit 0 = hits, 1 = no match, 2 = usage/config/transport error

Configuration (environment) - same transports as s3search.py:
  S3G_ROOT     archive root: local path | file://path | ssh://user@host/path | s3://bucket/prefix | s3://
               (plain "s3://" = the first glob level matches real bucket NAMES in the account)
  S3G_LAYOUT   layout under the root, default "{bucket}/*{day}*" ; placeholders {bucket} {day} {yyyy} {mm} {dd}
               e.g. "{bucket}/{yyyy}/{mm}/{dd}/*"
  S3S_SSH_OPTS / S3S_CALL_LOG as in s3search.py
"""
import fnmatch, os, re, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from s3search import S3, check_globs, die, emit, transport   # noqa: E402


def main(argv):
    if os.environ.get("S3S_CALL_LOG"):
        with open(os.environ["S3S_CALL_LOG"], "a") as f:
            f.write("s3grep " + " ".join(argv) + "\n")
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    if argv[0] != "find" or len(argv) != 4:
        die("usage: s3grep.py find <literal> <bucket-glob> <YYYY-MM-DD>")
    literal, bglob, day = argv[1:]
    check_globs(bglob)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", day)
    if not m or not literal:
        die("find: need a non-empty literal and a YYYY-MM-DD day")
    yyyy, mm, dd = m.groups()
    layout = os.environ.get("S3G_LAYOUT", "{bucket}/*{day}*")
    root = os.environ.get("S3G_ROOT", "")
    if root.rstrip("/") == "s3:":                       # account-wide: glob over bucket names
        try:
            names = [l.split()[-1] for l in subprocess.run(["aws", "s3", "ls"], capture_output=True,
                                                           text=True, check=True).stdout.splitlines() if l.strip()]
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            die("aws s3 ls failed: %s" % e)
        rest = layout.split("/", 1)[1] if "/" in layout else "*"
        rest = rest.format(day=day, yyyy=yyyy, mm=mm, dd=dd)

        def gen():
            for b in names:
                if fnmatch.fnmatch(b, bglob):
                    for _, line in S3(b, "").lines(rest, literal):
                        yield line
        return 0 if emit(gen()) else 1
    t = transport(root)
    pattern = layout.format(bucket=bglob, day=day, yyyy=yyyy, mm=mm, dd=dd).replace("**", "*")
    return 0 if emit(l for _, l in t.lines(pattern, literal)) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
