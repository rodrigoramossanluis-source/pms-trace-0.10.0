#!/usr/bin/env python3
# SPDX-License-Identifier: MIT  -  Copyright (c) 2026 Rodrigo Ramos
"""s3search.py - reference log-search backend for pms_trace.sh.

It only STRUCTURES a search: it maps (system, component, hour) to log files and filters lines.
It does not index, cache or modify anything. Works on a local folder, any SSH host
(standard `ssh` + `find`/`grep`/`gzip` on the remote) or an S3 bucket (AWS CLI v2).

  s3search.py ls    <sys> <comp>                    list "<sys>/<comp>" apps (globs allowed)
  s3search.py grep  <sys> <comp> <blob> <literal>   raw lines containing <literal>
  s3search.py query <sys> <comp> <blob> "<expr>"    JSON-field filter, e.g.
                    s.flowToken = "abc" AND s.message LIKE "%M-123%"
  blob = YYYY-MM-DD-HH (one hour) or YYYY-MM-DD-* (whole day)
  exit 0 = hits, 1 = no match, 2 = usage/config/transport error

Configuration (environment):
  S3S_ROOT     where the logs are (required):
                 /var/log/archive  or  file:///var/log/archive
                 ssh://user@host/var/log/archive    (key-based auth; uses ~/.ssh/config)
                 s3://bucket/prefix                 (uses AWS_PROFILE / AWS_REGION as usual)
  S3S_LAYOUT   file layout under the root; must start with {sys}/{comp}/
                 default "{sys}/{comp}/{date}-{hour}*"   e.g. cm-ota2/booking-gateway/2026-01-10-12.log.gz
                 other: "{sys}/{comp}/{yyyy}/{mm}/{dd}/{hour}/*"
                 placeholders: {sys} {comp} {date}=YYYY-MM-DD {yyyy} {mm} {dd} {hour}=HH (or * for a whole day)
  S3S_SSH_OPTS extra ssh options (default "-o BatchMode=yes -o ConnectTimeout=15")
  S3S_CALL_LOG optional file; every invocation is appended (audit / cost counting)
Files ending in .gz are decompressed transparently.
"""
import fnmatch, gzip, io, json, os, re, shlex, subprocess, sys

SAFE = re.compile(r"^[A-Za-z0-9._*?\-\[\]]+$")      # sys / comp / bucket globs (no spaces, no slashes)
BLOB = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(\d{2}|\*)$")


def die(msg, rc=2):
    print("[s3search] " + msg, file=sys.stderr)
    sys.exit(rc)


# ---------------------------------------------------------------- transports
class Local:
    def __init__(self, root):
        self.root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(self.root):
            die("root not found: " + self.root)

    def dirs(self, sys_glob, comp_glob):
        out = []
        for s in sorted(os.listdir(self.root)):
            if not fnmatch.fnmatch(s, sys_glob) or not os.path.isdir(os.path.join(self.root, s)):
                continue
            for c in sorted(os.listdir(os.path.join(self.root, s))):
                if fnmatch.fnmatch(c, comp_glob) and os.path.isdir(os.path.join(self.root, s, c)):
                    out.append("%s/%s" % (s, c))
        return out

    def files(self, pattern):
        import glob
        return sorted(p for p in glob.glob(os.path.join(self.root, pattern)) if os.path.isfile(p))

    def lines(self, pattern, literal):
        for p in self.files(pattern):
            opener = gzip.open if p.endswith(".gz") else open
            with opener(p, "rt", errors="replace") as f:
                for line in f:
                    if literal in line:
                        yield p[len(self.root) + 1:], line.rstrip("\n")


class SSH:
    """One ssh round-trip per call; the filtering (grep -F) runs on the remote host."""

    def __init__(self, target, root):
        self.target, self.root = target, root or "."
        self.opts = shlex.split(os.environ.get("S3S_SSH_OPTS", "-o BatchMode=yes -o ConnectTimeout=15"))

    def _run(self, script):
        cmd = ["ssh", "-n"] + self.opts + [self.target, "sh -c " + shlex.quote(script)]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
        for line in p.stdout:
            yield line.rstrip("\n")
        p.wait()
        if p.returncode not in (0, 1):
            die("ssh %s failed rc=%s: %s" % (self.target, p.returncode, p.stderr.read().strip()[:300]))

    def dirs(self, sys_glob, comp_glob):
        s = "cd %s || exit 2; for d in %s/%s; do [ -d \"$d\" ] && printf '%%s\\n' \"$d\"; done; exit 0" % (
            shlex.quote(self.root), sys_glob, comp_glob)
        return sorted(self._run(s))

    def lines(self, pattern, literal):
        # pattern is built only from validated globs, so it is safe unquoted (the remote shell expands it)
        s = ("cd %s || exit 2; for f in %s; do [ -f \"$f\" ] || continue; "
             "case \"$f\" in *.gz) gzip -dc \"$f\";; *) cat \"$f\";; esac | grep -F -e %s | awk -v f=\"$f\" '{print f \"\\t\" $0}'; done; exit 0"
             % (shlex.quote(self.root), pattern, shlex.quote(literal)))
        for l in self._run(s):
            path, _, line = l.partition("\t")
            yield path, line


class S3:
    """AWS CLI transport. Objects are streamed (aws s3 cp <obj> -) and filtered locally."""

    def __init__(self, bucket, prefix):
        self.bucket, self.prefix = bucket, prefix.strip("/")

    def _aws(self, *args):
        try:
            p = subprocess.run(["aws", "s3"] + list(args), capture_output=True, text=True)
        except FileNotFoundError:
            die("aws CLI not found (install AWS CLI v2)")
        if p.returncode not in (0, 1):
            die("aws s3 %s failed: %s" % (" ".join(args), p.stderr.strip()[:300]))
        return p.stdout.splitlines()

    def _url(self, rel=""):
        key = "/".join(x for x in (self.prefix, rel) if x)
        return "s3://%s/%s" % (self.bucket, key + "/" if key else "")

    def _subdirs(self, rel):
        return [l.split("PRE ", 1)[1].rstrip("/") for l in self._aws("ls", self._url(rel)) if "PRE " in l]

    def dirs(self, sys_glob, comp_glob):
        out = []
        for s in self._subdirs(""):
            if fnmatch.fnmatch(s, sys_glob):
                out += ["%s/%s" % (s, c) for c in self._subdirs(s) if fnmatch.fnmatch(c, comp_glob)]
        return sorted(out)

    def files(self, pattern):
        static = re.split(r"[*?\[]", pattern, 1)[0]            # list only below the non-glob part
        base = static.rsplit("/", 1)[0] if "/" in static else ""
        keys = []
        for l in self._aws("ls", "--recursive", self._url(base)):
            parts = l.split(None, 3)
            if len(parts) == 4:
                rel = parts[3][len(self.prefix) + 1:] if self.prefix else parts[3]
                if fnmatch.fnmatch(rel, pattern):
                    keys.append(rel)
        return sorted(keys)

    def lines(self, pattern, literal):
        for rel in self.files(pattern):
            url = "s3://%s/%s" % (self.bucket, "/".join(x for x in (self.prefix, rel) if x))
            p = subprocess.Popen(["aws", "s3", "cp", url, "-"], stdout=subprocess.PIPE)
            stream = gzip.GzipFile(fileobj=p.stdout) if rel.endswith(".gz") else p.stdout
            for raw in io.TextIOWrapper(stream, errors="replace"):
                if literal in raw:
                    yield rel, raw.rstrip("\n")
            p.wait()


def transport(root):
    if not root:
        die("S3S_ROOT is not set (local path, ssh://user@host/path or s3://bucket/prefix)")
    if root.startswith("ssh://"):
        rest = root[6:]
        target, _, path = rest.partition("/")
        return SSH(target, "/" + path if path else ".")
    if root.startswith("s3://"):
        bucket, _, prefix = root[5:].partition("/")
        return S3(bucket, prefix)
    return Local(root[7:] if root.startswith("file://") else root)


# ---------------------------------------------------------------- helpers
def layout_glob(layout, sys_, comp, blob):
    m = BLOB.match(blob)
    if not m:
        die("blob must be YYYY-MM-DD-HH or YYYY-MM-DD-* (got %r)" % blob)
    yyyy, mm, dd, hh = m.groups()
    out = layout.format(sys=sys_, comp=comp, date="%s-%s-%s" % (yyyy, mm, dd), yyyy=yyyy, mm=mm, dd=dd, hour=hh)
    return out.replace("**", "*")


def check_globs(*vals):
    for v in vals:
        if not SAFE.match(v):
            die("unsafe or empty name %r (allowed: letters, digits, . _ - and globs * ? [])" % v)


def emit(lines):
    n = 0
    try:
        for line in lines:
            sys.stdout.write(line + "\n")
            n += 1
        sys.stdout.flush()
    except BrokenPipeError:                       # caller capped output (head): that is still evidence
        try:
            sys.stdout = None
        finally:
            os._exit(141)
    return n


COND = re.compile(r"""s\.([A-Za-z0-9_.@-]+)\s*(=|LIKE)\s*(?:"([^"]*)"|'([^']*)')""", re.I)


def parse_query(expr):
    conds = [(f, op.upper(), a or b) for f, op, a, b in COND.findall(expr)]
    if not conds:
        die("query: no conditions found in %r (use s.field = \"x\" [AND s.f LIKE \"%%y%%\"])" % expr)
    if re.search(r"\bOR\b", expr, re.I):
        die("query: only AND is supported")
    return conds


def field(obj, dotted):
    for part in dotted.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


def matches(line, conds):
    try:
        obj = json.loads(line[line.index("{"):]) if "{" in line else None
    except ValueError:
        obj = None
    for f, op, v in conds:
        val = field(obj, f) if isinstance(obj, dict) else None
        if val is None:                               # non-JSON line / missing field: substring fallback
            if v.strip("%") not in line:
                return False
            continue
        sval = val if isinstance(val, str) else json.dumps(val)
        if op == "=" and sval != v:
            return False
        if op == "LIKE" and not fnmatch.fnmatchcase(sval, v.replace("%", "*").replace("_", "?")):
            return False
    return True


# ---------------------------------------------------------------- main
def main(argv):
    if os.environ.get("S3S_CALL_LOG"):
        with open(os.environ["S3S_CALL_LOG"], "a") as f:
            f.write(" ".join(argv) + "\n")
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    cmd, a = argv[0], argv[1:]
    layout = os.environ.get("S3S_LAYOUT", "{sys}/{comp}/{date}-{hour}*")
    if not layout.startswith("{sys}/{comp}/"):
        die("S3S_LAYOUT must start with {sys}/{comp}/")
    t = transport(os.environ.get("S3S_ROOT", ""))
    if cmd == "ls" and len(a) == 2:
        check_globs(*a)
        return 0 if emit(t.dirs(a[0], a[1])) else 1
    if cmd == "grep" and len(a) == 4:
        check_globs(a[0], a[1])
        if not a[3]:
            die("grep: empty pattern")
        return 0 if emit(l for _, l in t.lines(layout_glob(layout, *a[:3]), a[3])) else 1
    if cmd == "query" and len(a) == 4:
        check_globs(a[0], a[1])
        conds = parse_query(a[3])
        prefilter = max((v.strip("%") for _, _, v in conds), key=len)
        if not prefilter:
            die("query: needs at least one non-empty value")
        hits = (l for _, l in t.lines(layout_glob(layout, *a[:3]), prefilter) if matches(l, conds))
        return 0 if emit(hits) else 1
    die("usage: s3search.py ls <sys> <comp> | grep <sys> <comp> <blob> <literal> | query <sys> <comp> <blob> <expr>")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
