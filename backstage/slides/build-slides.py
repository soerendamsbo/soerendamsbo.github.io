"""Build backstage/slides/slides.yml from the HTML decks in the misc-slides repo.

Run from the site root when decks are added:  python3 -I backstage/slides/build-slides.py
(add --full to re-read every deck, e.g. after changing the rules below).
Standard library only. Reads the gh-pages branch of soerendamsbo/misc-slides on
GitHub: the file list, each deck's <title>, and its date: the date printed on the
title slide, else the date in the file's metadata, else the file's last commit.
It also checks that the local files each deck refers to (images, styles, scripts)
exist in the repo, and ends with a report of what is missing. Decks unchanged since
the last run (same git blob sha) are taken from slides.yml without re-reading.
Decks: the .html files at the repo root (talks) and in teaching-decks/<folder>/, one
folder per course and semester; deeper folders (Quarto's *_files/ etc.) are ignored.
Talks are listed newest first; each course oldest first, then courses newest first.
The output is generated; don't edit it by hand.
"""

import html
import json
import posixpath
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

REPO = "soerendamsbo/misc-slides"
BRANCH = "gh-pages"
PAGES = "https://soerendamsbo.github.io/misc-slides/"
SKIP = {"template.html", "rladies-theme.html"}  # empty template and theme demo, not talks
TEACHING = "teaching-decks/"
# Headings for the teaching folders; a folder not listed here shows its folder name.
# Course name as it was taught (Danish), semester in English.
GROUPS = {
    "dak2-f2022": "Dansk og Komparativ Politik 2 · spring 2022",
    "dak2-f2023": "Dansk og Komparativ Politik 2 · spring 2023",
    "methods1-f2025": "Metode 1 · spring 2025",
    "methods1-f2026": "Metode 1 · spring 2026",
}
OUT = Path(__file__).with_name("slides.yml")

MONTHS = {m: i for names in (
    "januar februar marts april maj juni juli august september oktober november december",
    "january february march april may june july august september october november december")
    for i, m in enumerate(names.split(), 1)}
MONTH = "(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + ")"
# Where the title slide starts: Quarto/reveal.js, then xaringan's markdown source
TITLE_SLIDE = re.compile(r'id="title-slide"|class: [^\n]*title-slide')
# Local references: HTML attributes, CSS url(), markdown images (xaringan source)
REFS = re.compile(r'(?:src|href|data-src|data-background-image|poster)="([^"]+)"'
                  r"|url\(\s*[\"']?([^\"')]+)[\"']?\s*\)"
                  r"|!\[[^\]]*\]\(([^)\s]+)\)")
EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|#)", re.I)  # https:, data:, mailto:, //cdn, #anchor


def get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "build-slides", **(headers or {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def title_slide_date(text):
    """Date printed on the title slide: YYYY-MM-DD, or YYYY-MM when it gives only a month."""
    m = TITLE_SLIDE.search(text)
    if not m:
        return None
    slide = re.split(r"</section>|\n---\s*\n", text[m.start():m.start() + 20000])[0]
    slide = html.unescape(re.sub(r"<[^>]+>", " ", slide)).lower()
    full = [
        (r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", lambda g: (g[0], g[1], g[2])),
        (r"\b(\d{1,2})-(\d{1,2})-(\d{4})\b", lambda g: (g[2], g[1], g[0])),
        (r"\b(\d{1,2})\.?\s+" + MONTH + r",?\s+(\d{4})\b", lambda g: (g[2], MONTHS[g[1]], g[0])),
        (MONTH + r"\s+(\d{1,2}),?\s+(\d{4})\b", lambda g: (g[2], MONTHS[g[0]], g[1])),
    ]
    for pattern, ymd in full:
        m = re.search(pattern, slide)
        if m:
            y, mo, d = (int(x) for x in ymd(m.groups()))
            return date(y, mo, d).isoformat()
    m = re.search(MONTH + r",?\s+(\d{4})\b", slide)
    return f"{int(m.group(2)):04d}-{MONTHS[m.group(1)]:02d}" if m else None


def metadata_date(text):
    """Date in the deck's <meta> tags (Quarto: dcterms.date; R Markdown: date), or None."""
    m = re.search(r'<meta name="(?:dcterms\.date|date)" content="(\d{4}-\d{2}-\d{2})', text)
    return m.group(1) if m else None


def last_commit_date(name):
    url = f"https://api.github.com/repos/{REPO}/commits?sha={BRANCH}&path={urllib.parse.quote(name)}&per_page=1"
    try:
        commits = json.loads(get(url))
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            sys.exit("GitHub's API limit reached (60 calls an hour without login): run again in an hour.")
        raise
    return commits[0]["commit"]["committer"]["date"][:10] if commits else None


def local_refs(text):
    """Local files the deck refers to, as written. HTML comments and script bodies
    are skipped: commented-out slides and code strings are not references."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"(<script\b[^>]*>).*?(</script>)", r"\1\2", text, flags=re.S | re.I)
    refs = set()
    for m in REFS.finditer(text):
        ref = html.unescape(next(g for g in m.groups() if g)).strip()
        ref = urllib.parse.unquote(ref.split("#")[0].split("?")[0])
        if ref and not EXTERNAL.match(ref) and not ref.endswith("/") \
                and not re.search(r"[\s{}<>$+'`\\]", ref):
            refs.add(ref)
    return sorted(refs)


def check_refs(name, refs, repo_files):
    """(missing, absolute): relative refs not found in the repo, and refs starting
    with "/", which resolve against the main site rather than misc-slides."""
    missing, absolute = [], []
    for ref in refs:
        if ref.startswith("/"):
            absolute.append(ref)
        elif posixpath.normpath(posixpath.join(posixpath.dirname(name), ref)) not in repo_files:
            missing.append(ref)
    return missing, absolute


def is_deck(path):
    """Root-level .html files, and .html files directly in a teaching-decks/<folder>/."""
    parts = path.split("/")
    if not path.endswith(".html") or parts[-1] in SKIP:
        return False
    if len(parts) == 1:
        return path != "index.html"
    return len(parts) == 3 and path.startswith(TEACHING)


def group_of(path):
    """'Talks' for root-level decks, else the teaching folder's heading."""
    if "/" not in path:
        return "Talks"
    folder = path.split("/")[1]
    return GROUPS.get(folder, folder)


def natural(s):
    """Sort key that puts lecture-2 before lecture-10."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def ordered(decks):
    """Talks newest first; then each course oldest first, courses newest first."""
    by_group = {}
    for d in decks:
        by_group.setdefault(d["group"], []).append(d)
    talks = sorted(by_group.pop("Talks", []),
                   key=lambda d: (d["when"] or "", natural(d["file"])), reverse=True)
    courses = [sorted(g, key=lambda d: (d["when"] or "", natural(d["file"])))
               for g in by_group.values()]
    courses.sort(key=lambda g: max(d["when"] or "" for d in g), reverse=True)
    return talks + [d for g in courses for d in g]


def read_previous():
    """Entries from the last run's slides.yml, by file name."""
    prev, cur = {}, None
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            m = re.match(r"(- |  )(\w+): (.*)$", line)
            if m:
                value = json.loads(m.group(3))
                if m.group(1) == "- ":
                    cur = prev.setdefault(value, {})
                cur[m.group(2)] = value
    return prev


def read_deck(name, sha):
    text = get(f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{urllib.parse.quote(name)}")
    text = text.decode("utf-8", errors="replace")
    m = re.search(r"<title>(.*?)</title>", text, re.S)
    title = html.unescape(m.group(1)).strip() if m else ""
    when, source = title_slide_date(text), "title slide"
    if not when:
        when, source = metadata_date(text), "metadata"
    if not when:
        when, source = last_commit_date(name), "last commit"
    return {"file": name, "title": title or name, "when": when, "date_source": source,
            "url": PAGES + urllib.parse.quote(name), "sha": sha, "refs": local_refs(text)}


def main():
    tree = json.loads(get(f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}?recursive=1"))
    if tree.get("truncated"):
        sys.exit("GitHub truncated the file list, so the file check would be unreliable.")
    repo_files = {f["path"] for f in tree["tree"] if f["type"] == "blob"}
    shas = {f["path"]: f["sha"] for f in tree["tree"] if f["type"] == "blob" and is_deck(f["path"])}
    prev = {} if "--full" in sys.argv[1:] else read_previous()
    decks = []
    for name in sorted(shas):
        old = prev.get(name)
        reused = bool(old) and old.get("sha") == shas[name] and "refs" in old
        d = dict(old) if reused else read_deck(name, shas[name])
        # Set every run: copied-in files clear a warning, and GROUPS edits apply
        d["group"] = group_of(name)
        d["missing"], d["absolute"] = check_refs(name, d["refs"], repo_files)
        decks.append(d)
        print(f"{d['when']}  {d['date_source']:11}  {'(unchanged) ' if reused else ''}{name}", file=sys.stderr)
    decks = ordered(decks)

    lines = ["# Generated by backstage/slides/build-slides.py from github.com/" + REPO + " (" + BRANCH + ").",
             "# Don't edit by hand: re-run the script. Order: talks, then courses (see the script).",
             "# group: Talks, or the teaching folder's heading. date_source: title slide | metadata | last commit",
             "# when: YYYY-MM-DD, or YYYY-MM when the title slide gives only a month",
             "# sha: git blob of the deck (unchanged decks are not re-read); refs: local files it",
             "# refers to; missing: refs not in the repo; absolute: refs starting with /", ""]
    for d in decks:
        lines.append(f"- file: {json.dumps(d['file'])}")
        for key in ("group", "title", "when", "date_source", "url", "sha", "refs", "missing", "absolute"):
            lines.append(f"  {key}: {json.dumps(d[key], ensure_ascii=False)}")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(decks)} decks to {OUT}\n", file=sys.stderr)

    # Report. A deck with missing files still opens, just with blank images etc.
    flagged = sorted((d for d in decks if d["missing"] or d["absolute"]), key=lambda d: d["file"])
    for d in flagged:
        if d["missing"]:
            more = " ..." if len(d["missing"]) > 5 else ""
            print(f"! {d['file']}: {len(d['missing'])} missing file(s): {', '.join(d['missing'][:5])}{more}\n"
                  "    -> copy them into misc-slides, or re-render with embed-resources: true", file=sys.stderr)
        if d["absolute"]:
            more = " ..." if len(d["absolute"]) > 3 else ""
            print(f"! {d['file']}: {len(d['absolute'])} path(s) starting with \"/\", which point to the main "
                  f"site, not misc-slides: {', '.join(d['absolute'][:3])}{more}\n"
                  "    -> drop the leading / in the source and re-render", file=sys.stderr)
    print(f"OK: {len(decks) - len(flagged)} of {len(decks)} decks have all their files", file=sys.stderr)


if __name__ == "__main__":
    main()
