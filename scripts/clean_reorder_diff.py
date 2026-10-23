#!/usr/bin/env python3
"""Remove false-positive differences caused by list reordering from argocd-diff-preview output.

argocd-diff-preview diffs the rendered YAML line by line, so reordering list
entries (e.g. environment variables) shows up as a change. This script
post-processes the generated `diff.md` and/or `diff.html` and removes those
reorderings for the arrays selected by JSONPath expressions
(default: `..env,..volumes,..volumeMounts`).

How it works
------------
The output only contains hunks of the diff (changed lines plus context). For
every hunk the old text (context + removed lines) and the new text (context +
added lines) are reconstructed. In both texts, every array matched by one of the
JSONPaths is sorted (by `name` if available, otherwise by content). If both texts
are equal afterwards, the hunk only contained reorderings and is removed.
Otherwise the hunk is re-rendered so that only the real changes remain (shown in
the order of the target branch).

To never hide a real change, an array is only sorted when it is fully visible in
the hunk: its key line (e.g. `env:`) and its end must be inside the hunk. Arrays
that are cut off by a `@@ skipped ... @@` marker are left untouched. If reordered
arrays are long, run argocd-diff-preview with a bigger `--line-count` (e.g. 50)
and pass a smaller `--context` to this script to shrink the output again.

JSONPath matching
-----------------
Supported syntax: `.field`, `..field` (any depth), `.*` (any field), `[*]`,
`[n]`, `['field.with.dots']`, an optional leading `$` and kubectl-style `{...}`.
The path must end with the field that holds the array, for example
`.spec.template.spec.containers[*].env`.

A hunk usually does not show the whole path of an array (e.g. `spec:` and
`template:` are above the hunk). The visible part of the path must match
exactly, the invisible part is assumed to match. The indentation is always
checked, so `.spec.template.spec.containers[*].env` (Deployment) never matches
`.spec.jobTemplate.spec.template.spec.containers[*].env` (CronJob). However, if
e.g. the `containers:` line is not visible, `containers` and `initContainers`
cannot be told apart. This can only hide a reordering, never a content change.

Resources, applications and summary entries without remaining changes are
removed and the `(+x|-y)` statistics are updated.

Usage
-----
    python3 scripts/clean_reorder_diff.py output/diff.md output/diff.html
    python3 scripts/clean_reorder_diff.py \\
        --paths '.spec.template.spec.containers[*].env,.spec.template.spec.volumes' output/diff.md
    python3 scripts/clean_reorder_diff.py --line-count 50 --context 5 output/diff.md

Files are modified in place unless `--output` is given. The script needs the
same `--line-count` and `--diff-ignore` values that were used to generate the
output. They default to the LINE_COUNT and DIFF_IGNORE environment variables
(the same ones argocd-diff-preview reads) and otherwise to 5 and none.
"""

from __future__ import annotations

import argparse
import difflib
import html
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_PATHS = "..env,..volumes,..volumeMounts"

# Lines argocd-diff-preview always ignores (see defaultIgnorePatterns in pkg/matching/output.go)
DEFAULT_IGNORE_PREFIXES = (
    "app.kubernetes.io/version: ",
    "helm.sh/chart: ",
    "checksum/config: ",
    "checksum/rules: ",
    "checksum/certs: ",
    "checksum/cmd-params: ",
    "checksum/cm: ",
    "checksum/config-maps: ",
    "checksum/secret: ",
    "checksum/secrets: ",
    "caBundle: ",
    "ca.crt: ",
    "tls.crt: ",
    "tls.key: ",
)

MARKER_RE = re.compile(r"^@@ skipped (\d+) lines(?: \((\d+) -> (\d+)\))? @@$")
_KEY = r"(?P<key>\"[^\"]*\"|'[^']*'|[^\s:\"'#][^:]*?)"
KEY_ONLY_RE = re.compile(r"^(?P<indent> *)(?P<dash>- )?" + _KEY + r":$")
KEY_RE = re.compile(r"^(?P<indent> *)(?P<dash>- )?" + _KEY + r":(?:\s|$)")
BLOCK_SCALAR_RE = re.compile(r"^(?P<indent> *)(?P<dash>- )?(?:" + _KEY + r":\s+)?[|>][-+0-9]*\s*$")
ITEM_RE = re.compile(r"^( *)-(?: |$)")
NAME_RE = re.compile(r"^name:\s+(.*?)\s*$")

SUMMARY_HEADER_RE = re.compile(r"^(Added|Deleted|Modified) \((\d+)\):$")
SUMMARY_ENTRY_RE = re.compile(r"^(?P<sym>[+\-±]) (?P<name>.*?)(?: \((?P<stats>\+\d+\|-\d+|\+\d+|-\d+)\))?$")
SECTION_NAME_RE = re.compile(r'^(?P<name>.*?)(?: \[<a href=".*?">link</a>\])? \((?P<path>.*)\)$')

MD_TRUNCATED_WARNING = "🚨 Diff is too long"
MD_RENAMED_EMPTY = "_Application name changed, but rendered resources are unchanged_"
HTML_RENAMED_EMPTY = "<p><em>Application name changed, but rendered resources are unchanged</em></p>"
HTML_H4_RE = re.compile(r'^<h4 class="resource_header">(.*)</h4>$')
HTML_ROW_RE = re.compile(r'^\t<tr class="\w+"><td><pre>(.*)</pre></td></tr>$')
HTML_SKIPPED = "<p><em>Skipped</em></p>"


# ---------------------------------------------------------------------------
# JSONPath selection of arrays
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class _Node:
    """A visible YAML node on the path to an array: a mapping field or an array item."""

    col: int
    kind: str  # "field" | "item"
    name: Optional[str] = None
    index: Optional[int] = None  # position of an item, None if unknown
    children: int = 0


@dataclass(frozen=True)
class _Token:
    kind: str  # "field" | "index" | "any" (recursive descent)
    name: Optional[str] = None  # None: any field
    index: Optional[int] = None  # None: [*]


@dataclass(frozen=True)
class ArrayPath:
    raw: str
    tokens: tuple

    def matches(self, chain: tuple) -> bool:
        """Matches the visible path of an array (`chain`, root side first) from the end.

        Nodes above the first visible node are unknown and assumed to match, as long
        as the indentation of the visible nodes fits the depth given by the path.
        """
        tokens = self.tokens

        def rec(vi: int, pi: int) -> bool:
            if pi < 0:
                return vi < 0
            token = tokens[pi]
            if token.kind == "any":
                return rec(vi, pi - 1) or (vi >= 0 and rec(vi - 1, pi))
            if vi < 0:
                return True
            node = chain[vi]
            expected = _expected_col(tokens, pi)
            if expected is not None and expected != node.col:
                return False
            if token.kind == "field":
                if node.kind != "field" or (token.name is not None and token.name != node.name):
                    return False
            elif node.kind != "item" or (
                token.index is not None and node.index is not None and token.index != node.index
            ):
                return False
            return rec(vi - 1, pi - 1)

        return rec(len(chain) - 1, len(tokens) - 1)


def _expected_col(tokens: tuple, k: int) -> Optional[int]:
    """Column of tokens[k] in the YAML written by argocd-diff-preview (sigs.k8s.io/yaml).

    Nested fields are indented by 2, array items start at the column of their key
    and the content of an item is indented by 2 ("- ").
    """
    if any(t.kind == "any" for t in tokens[: k + 1]):
        return None
    col = 0
    for t in range(1, k + 1):
        col += 0 if (tokens[t - 1].kind == "field" and tokens[t].kind == "index") else 2
    return col


def _read_name(p: str, start: int) -> tuple[str, int]:
    end = start
    while end < len(p) and p[end] not in ".[":
        end += 1
    return p[start:end].strip(), end


def parse_path(raw: str) -> ArrayPath:
    p = raw.strip()
    if p.startswith("{") and p.endswith("}"):
        p = p[1:-1].strip()
    if p.startswith("$"):
        p = p[1:]

    def fail(msg: str) -> ArrayPath:
        raise ValueError(f"invalid JSONPath {raw.strip()!r}: {msg}")

    if not p:
        return fail("path is empty")
    if p[0] not in ".[":
        p = "." + p

    tokens: list[_Token] = []
    i = 0
    while i < len(p):
        if p.startswith("..", i):
            name, i = _read_name(p, i + 2)
            if not name:
                return fail("'..' must be followed by a field name")
            tokens += [_Token("any"), _Token("field", None if name == "*" else name)]
        elif p[i] == ".":
            name, i = _read_name(p, i + 1)
            if not name:
                return fail(f"missing field name at position {i}")
            tokens.append(_Token("field", None if name == "*" else name))
        elif p[i] == "[":
            end = p.find("]", i)
            if end < 0:
                return fail("unclosed '['")
            content = p[i + 1 : end].strip()
            i = end + 1
            if content == "*":
                tokens.append(_Token("index"))
            elif len(content) >= 2 and content[0] in "'\"" and content[-1] == content[0]:
                tokens.append(_Token("field", content[1:-1]))
            elif content.isdigit():
                tokens.append(_Token("index", index=int(content)))
            else:
                return fail(f"unsupported expression [{content}] (use [*], [n] or ['field'])")
        else:
            return fail(f"unexpected character {p[i]!r} at position {i}")

    if tokens[-1].kind != "field":
        return fail("the path must end with the field that holds the array, e.g. .spec.template.spec.containers[*].env")
    return ArrayPath(raw.strip(), tuple(tokens))


def parse_paths(s: str) -> list[ArrayPath]:
    """Parses a comma or newline separated list of JSONPath expressions."""
    return [parse_path(p) for p in re.split(r"[,\n]", s) if p.strip()]


@dataclass
class Config:
    paths: list[ArrayPath]
    all_lists: bool = False
    line_count: int = 5
    context: int = 5
    keep_mixed_hunks: bool = False
    ignore_regex: Optional[re.Pattern] = None

    def is_hidden(self, text: str) -> bool:
        """Mirrors shouldShowLine() of argocd-diff-preview for changed lines."""
        if self.ignore_regex is not None and self.ignore_regex.search(text):
            return True
        stripped = text.lstrip(" \t")
        return any(stripped.startswith(p) for p in DEFAULT_IGNORE_PREFIXES)

    def sorts(self, chain: Optional[tuple]) -> bool:
        if self.all_lists:
            return True
        return chain is not None and any(p.matches(chain) for p in self.paths)


@dataclass
class Stats:
    hunks_removed: int = 0
    hunks_rewritten: int = 0
    resources_removed: int = 0
    apps_removed: int = 0

    def add(self, other: "Stats") -> None:
        self.hunks_removed += other.hunks_removed
        self.hunks_rewritten += other.hunks_rewritten
        self.resources_removed += other.resources_removed
        self.apps_removed += other.apps_removed


# ---------------------------------------------------------------------------
# YAML list normalization (indentation based, works on partial YAML)
# ---------------------------------------------------------------------------


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank(line: str) -> bool:
    return line.strip() == ""


def _item_indent(line: str) -> Optional[int]:
    m = ITEM_RE.match(line)
    return len(m.group(1)) if m else None


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _block_scalar_threshold(line: str) -> Optional[int]:
    """Returns the indentation the content of a block scalar (`key: |`) must exceed."""
    m = BLOCK_SCALAR_RE.match(line)
    if not m:
        return None
    indent = len(m.group("indent"))
    if m.group("dash") and m.group("key") is not None:
        return indent + 2
    return indent


def _item_name(item: list[str], indent: int) -> Optional[str]:
    top = indent + 2
    for idx, line in enumerate(item):
        if idx == 0:
            content = line[top:]
        elif _indent(line) == top and not line[top:].startswith("-"):
            content = line[top:]
        else:
            continue
        m = NAME_RE.match(content)
        if m:
            return _unquote(m.group(1))
    return None


def _item_sort_key(item: list[str], indent: int) -> tuple:
    name = _item_name(item, indent)
    text = "\n".join(item)
    return (0, name, text) if name is not None else (1, "", text)


def _item_identity(name: Optional[str], item: list[str]) -> str:
    return f"name:{name}" if name is not None else "text:" + "\n".join(item)


CANONICAL, COLLECT, FOLLOW = "canonical", "collect", "follow"


def _structure(lines: list[str]) -> dict[int, tuple]:
    """Returns the visible path (root side first) of every key line, keyed by line index."""
    chains: dict[int, tuple] = {}
    stack: list[_Node] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        dash = _item_indent(line)
        m = KEY_RE.match(line)
        if _is_blank(line) or (dash is None and m is None):
            # Blank line or continuation of a multi-line value
            i += 1
            continue
        if dash is not None:
            while stack and (stack[-1].col > dash or (stack[-1].col == dash and stack[-1].kind == "item")):
                stack.pop()
            parent = stack[-1] if stack and stack[-1].kind == "field" and stack[-1].col in (dash, dash - 2) else None
            index = None
            if parent is not None:
                index = parent.children
                parent.children += 1
            stack.append(_Node(dash, "item", index=index))
        if m is not None:
            col = len(m.group("indent")) + (2 if m.group("dash") else 0)
            while stack and stack[-1].col >= col:
                stack.pop()
            stack.append(_Node(col, "field", name=_unquote(m.group("key"))))
            chains[i] = tuple(stack)
        threshold = _block_scalar_threshold(line)
        i += 1
        if threshold is not None:
            while i < n and (_is_blank(lines[i]) or _indent(lines[i]) > threshold):
                i += 1
    return chains


def normalize(
    lines: list[str],
    closed: bool,
    cfg: Config,
    mode: str = CANONICAL,
    orders: Optional[dict] = None,
    path: tuple = (),
    counters: Optional[dict] = None,
    chains: Optional[dict] = None,
    offset: int = 0,
) -> list[str]:
    """Walks all fully visible arrays selected by the configured JSONPaths.

    mode CANONICAL sorts them, COLLECT records their item order into `orders`
    and FOLLOW reorders them like the matching array recorded in `orders`.

    `closed` tells whether `lines` ends at a real structural boundary (end of
    the document). If not, an array that runs until the last line may continue
    outside the visible part and is therefore left alone.

    `chains` holds the visible path of every key line of the top-level `lines`;
    `offset` maps line indexes of nested calls back to the top level.
    """
    if counters is None:
        counters = {}
    if chains is None:
        chains = _structure(lines)
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]

        threshold = _block_scalar_threshold(line)
        if threshold is not None:
            j = i + 1
            while j < n and (_is_blank(lines[j]) or _indent(lines[j]) > threshold):
                j += 1
            out.extend(lines[i:j])
            i = j
            continue

        m = KEY_ONLY_RE.match(line)
        if m and i + 1 < n:
            key_col = len(m.group("indent")) + (2 if m.group("dash") else 0)
            item_col = _item_indent(lines[i + 1])
            if item_col is not None and item_col in (key_col, key_col + 2):
                key = _unquote(m.group("key"))
                # Lists are identified by their position in the structure, so the same
                # list can be found in the old and the new version of a hunk
                base = path + ((key_col, key),)
                occurrence = counters.get(base, 0)
                counters[base] = occurrence + 1
                list_path = base + (occurrence,)

                items: list[tuple[Optional[str], list[str]]] = []
                j = i + 1
                while j < n and _item_indent(lines[j]) == item_col:
                    k = j + 1
                    while k < n and (_is_blank(lines[k]) or _indent(lines[k]) > item_col):
                        k += 1
                    raw = lines[j:k]
                    name = _item_name(raw, item_col)
                    item_path = list_path + ((name,) if name is not None else (len(items),))
                    item = _normalize_item(
                        raw, item_col, k < n or closed, cfg, mode, orders, item_path, counters, chains, offset + j
                    )
                    items.append((name, item))
                    j = k

                if (j < n or closed) and len(items) > 1 and cfg.sorts(chains.get(offset + i)):
                    if mode == CANONICAL:
                        items.sort(key=lambda it: _item_sort_key(it[1], item_col))
                    elif mode == COLLECT:
                        orders[list_path] = [_item_identity(name, item) for name, item in items]
                    elif mode == FOLLOW and list_path in orders:
                        items = _follow_order(items, orders[list_path])
                out.append(line)
                for _, item in items:
                    out.extend(item)
                i = j
                continue

        out.append(line)
        i += 1
    return out


def _normalize_item(
    item: list[str],
    indent: int,
    closed: bool,
    cfg: Config,
    mode: str,
    orders,
    path: tuple,
    counters: dict,
    chains: dict,
    offset: int,
) -> list[str]:
    first = item[0]
    # Turn "- key: value" into "  key: value" so the item content is parsed like a mapping
    as_mapping = first[:indent] + " " + first[indent + 1 :]
    result = normalize([as_mapping] + item[1:], closed, cfg, mode, orders, path, counters, chains, offset)
    result[0] = first
    return result


def _follow_order(items: list[tuple[Optional[str], list[str]]], order: list[str]) -> list:
    """Orders items like `order`; items without a counterpart keep their original position."""
    remaining = list(enumerate(items))
    result = []
    for identity in order:
        for idx, (_, (name, item)) in enumerate(remaining):
            if _item_identity(name, item) == identity:
                result.append(remaining.pop(idx)[1])
                break
    for position, item in remaining:
        result.insert(min(position, len(result)), item)
    return result


# ---------------------------------------------------------------------------
# Diff content processing
# ---------------------------------------------------------------------------


@dataclass
class Gap:
    """Lines hidden between two hunks, rendered as `@@ skipped N lines (a -> b) @@`."""

    count: int
    start: Optional[int] = None
    end: Optional[int] = None

    def exact(self) -> bool:
        return self.start is not None and self.end is not None and self.end - self.start + 1 == self.count

    def merge(self, other: "Gap") -> "Gap":
        if other.count == 0:
            return self
        if self.count == 0:
            return other
        if self.exact() and other.exact() and self.end + 1 == other.start:
            return Gap(self.count + other.count, self.start, other.end)
        return Gap(self.count + other.count)

    def render(self) -> str:
        if self.exact():
            return f"@@ skipped {self.count} lines ({self.start} -> {self.end}) @@"
        return f"@@ skipped {self.count} lines @@"


@dataclass
class ResourceResult:
    lines: Optional[list[str]]  # None => resource has no remaining changes
    added_before: int
    deleted_before: int
    added_after: int
    deleted_after: int
    stats: Stats = field(default_factory=Stats)


def _count(entries: list[tuple[str, str]]) -> tuple[int, int]:
    return sum(op == "+" for op, _ in entries), sum(op == "-" for op, _ in entries)


def _line_diff(a: list[str], b: list[str]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            result.extend((" ", t) for t in a[i1:i2])
        else:
            result.extend(("-", t) for t in a[i1:i2])
            result.extend(("+", t) for t in b[j1:j2])
    return result


def _process_hunk(entries: list[tuple[str, str]], closed: bool, cfg: Config) -> Optional[list[tuple[str, str]]]:
    """Returns None to keep the hunk, [] to drop it, or new entries to replace it."""
    if not any(op == "+" for op, _ in entries) or not any(op == "-" for op, _ in entries):
        return None
    old = [t for op, t in entries if op != "+"]
    new = [t for op, t in entries if op != "-"]
    norm_old, norm_new = normalize(old, closed, cfg), normalize(new, closed, cfg)
    if norm_old == old and norm_new == new:
        return None
    if norm_old == norm_new:
        return []
    # Show the real changes in the actual order of the target: reorder only the old side like the new one
    orders: dict = {}
    normalize(new, closed, cfg, COLLECT, orders)
    display_old = normalize(old, closed, cfg, FOLLOW, orders)
    if display_old == old:
        return None
    diff = _line_diff(display_old, new)
    if not any(op != " " and not cfg.is_hidden(t) for op, t in diff):
        return []
    if cfg.keep_mixed_hunks:
        return None
    return diff


def _build_chunks(changed: list[int], total: int, context: int) -> list[tuple[int, int]]:
    """Same algorithm as buildChunks() in pkg/matching/diff.go."""
    chunks = []
    start = max(0, changed[0] - context)
    end = min(total - 1, changed[0] + context)
    for current in changed[1:]:
        if current - end <= 2 * context:
            end = min(total - 1, current + context)
        else:
            chunks.append((start, end))
            start = max(0, current - context)
            end = min(total - 1, current + context)
    chunks.append((start, end))
    return chunks


def clean_resource(lines: list[str], cfg: Config, truncated: bool = False) -> ResourceResult:
    hunks: list[list[tuple[str, str]]] = [[]]
    gaps: list[Gap] = []
    for line in lines:
        if line.startswith("@@"):
            m = MARKER_RE.match(line)
            if not m:
                return _untouched(lines)
            start = int(m.group(2)) if m.group(2) is not None else None
            end = int(m.group(3)) if m.group(3) is not None else None
            gaps.append(Gap(int(m.group(1)), start, end))
            hunks.append([])
        elif line[:1] in (" ", "+", "-"):
            hunks[-1].append((line[0], line[1:]))
        else:
            return _untouched(lines)

    if truncated or any(not h for h in hunks):
        return _untouched(lines)

    added_before, deleted_before = _count([e for h in hunks for e in h])

    # Original coordinates of each hunk (needed to keep `(a -> b)` of merged markers exact)
    starts: list[Optional[int]] = []
    for i, hunk in enumerate(hunks):
        if i > 0 and gaps[i - 1].end is not None:
            starts.append(gaps[i - 1].end + 1)
        elif i < len(gaps) and gaps[i].start is not None:
            starts.append(gaps[i].start - len(hunk))
        else:
            starts.append(None)

    stats = Stats()
    rendered: list[Optional[tuple[int, list[str], int, bool]]] = []
    changed_anything = False
    for i, hunk in enumerate(hunks):
        closed = False
        if i == len(hunks) - 1:
            shown = [k for k, (op, t) in enumerate(hunk) if op != " " and not cfg.is_hidden(t)]
            changes = shown or [k for k, (op, _) in enumerate(hunk) if op != " "]
            last = changes[-1] if changes else len(hunk) - 1
            # The last chunk ends `line-count` lines after the last change unless the document ends earlier
            closed = len(hunk) - 1 - last < cfg.line_count

        result = _process_hunk(hunk, closed, cfg)
        rewritten = result is not None
        entries = hunk if result is None else result
        if result == []:
            stats.hunks_removed += 1
            changed_anything = True
            rendered.append(None)
            continue
        if rewritten:
            stats.hunks_rewritten += 1
            changed_anything = True

        triggers = [k for k, (op, t) in enumerate(entries) if op != " " and not cfg.is_hidden(t)]
        if not triggers:
            triggers = [k for k, (op, _) in enumerate(entries) if op != " "]
        chunks = _build_chunks(triggers, len(entries), cfg.context)
        start = None if rewritten else starts[i]

        body: list[str] = []
        for c, (c_start, c_end) in enumerate(chunks):
            if c > 0:
                prev_end = chunks[c - 1][1]
                gap = Gap(c_start - prev_end - 1)
                if start is not None:
                    gap = Gap(gap.count, start + prev_end + 1, start + c_start - 1)
                body.append(gap.render())
            body.extend(op + t for op, t in entries[c_start : c_end + 1])
        lead, trail = chunks[0][0], len(entries) - 1 - chunks[-1][1]
        if lead or trail or len(chunks) > 1:
            changed_anything = True
        rendered.append((lead, body, trail, rewritten))

    if not changed_anything:
        return _untouched(lines)

    out: list[str] = []
    pending = Gap(0)
    for i, item in enumerate(rendered):
        if i > 0:
            pending = pending.merge(gaps[i - 1])
        start, length = starts[i], len(hunks[i])
        if item is None:
            dropped = Gap(length, start, start + length - 1) if start is not None else Gap(length)
            pending = pending.merge(dropped)
            continue
        lead, body, trail, rewritten = item
        if lead:
            exact = start is not None and not rewritten
            pending = pending.merge(Gap(lead, start, start + lead - 1) if exact else Gap(lead))
        if out and pending.count:
            out.append(pending.render())
        out.extend(body)
        pending = Gap(0)
        if trail:
            exact = start is not None and not rewritten
            end = start + length - 1 if start is not None else None
            pending = Gap(trail, end - trail + 1, end) if exact else Gap(trail)

    final_entries = [(l[0], l[1:]) for l in out if not l.startswith("@@")]
    added_after, deleted_after = _count(final_entries)
    if not out:
        stats.resources_removed = 1
    return ResourceResult(out or None, added_before, deleted_before, added_after, deleted_after, stats)


def _untouched(lines: list[str]) -> ResourceResult:
    added, deleted = _count([(l[0], l[1:]) for l in lines if l[:1] in "+-"])
    return ResourceResult(lines, added, deleted, added, deleted)


# ---------------------------------------------------------------------------
# Document processing (markdown + html)
# ---------------------------------------------------------------------------


@dataclass
class SectionResult:
    name: str
    lines: Optional[list[str]]  # None => remove application section
    delta_added: int = 0
    delta_deleted: int = 0
    stats: Stats = field(default_factory=Stats)


def _go_html_escape(s: str) -> str:
    """Same escaping as Go's html.EscapeString."""
    return (
        s.replace("&", "&amp;")
        .replace("'", "&#39;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&#34;")
    )


def _html_row(line: str) -> str:
    css = {"@": "comment_line", "-": "removed_line", "+": "added_line"}.get(line[:1], "normal_line")
    return f'\t<tr class="{css}"><td><pre>{_go_html_escape(line)}</pre></td></tr>'


@dataclass
class _Resource:
    raw: list[str]
    diff: Optional[list[str]]  # None for skipped resources


def _parse_md_body(body: list[str]) -> list[object]:
    tokens: list[object] = []
    i = 0
    while i < len(body):
        line = body[i]
        if line.startswith("#### ") and i + 1 < len(body) and body[i + 1] == "```diff":
            j = i + 2
            while j < len(body) and body[j] != "```":
                j += 1
            if j < len(body):
                tokens.append(_Resource(body[i : j + 1], body[i + 2 : j]))
                i = j + 1
                continue
        if line.startswith("#### ") and body[i + 1 : i + 4] == ["", "_Skipped_", ""]:
            tokens.append(_Resource(body[i : i + 4], None))
            i += 4
            continue
        tokens.append(line)
        i += 1
    return tokens


def _parse_html_body(body: list[str]) -> Optional[list[object]]:
    tokens: list[object] = []
    i = 0
    while i < len(body):
        line = body[i]
        if HTML_H4_RE.match(line):
            if body[i + 1 : i + 2] == [HTML_SKIPPED]:
                tokens.append(_Resource(body[i : i + 2], None))
                i += 2
                continue
            if body[i + 1 : i + 4] == ['<div class="diff_container">', "<table>", ""]:
                j = i + 4
                diff: list[str] = []
                while j < len(body) and body[j] != "</table>":
                    m = HTML_ROW_RE.match(body[j])
                    if not m:
                        return None
                    diff.append(html.unescape(m.group(1)))
                    j += 1
                if body[j : j + 2] != ["</table>", "</div>"]:
                    return None
                tokens.append(_Resource(body[i : j + 2], diff))
                i = j + 2
                continue
        tokens.append(line)
        i += 1
    return tokens


def _process_section(name: str, header: list[str], body: list[str], is_html: bool, cfg: Config) -> SectionResult:
    tokens = _parse_html_body(body) if is_html else _parse_md_body(body)
    if tokens is None:
        return SectionResult(name, header + body)

    resources = [t for t in tokens if isinstance(t, _Resource)]
    diff_resources = [r for r in resources if r.diff is not None]
    truncated = not is_html and MD_TRUNCATED_WARNING in body
    last_diff = diff_resources[-1] if diff_resources else None

    result = SectionResult(name, None)
    new_body: list[str] = []
    kept = 0
    for token in tokens:
        if not isinstance(token, _Resource):
            new_body.append(token)
            continue
        if token.diff is None:
            new_body.extend(token.raw)
            kept += 1
            continue
        res = clean_resource(token.diff, cfg, truncated=truncated and token is last_diff)
        result.stats.add(res.stats)
        result.delta_added += res.added_before - res.added_after
        result.delta_deleted += res.deleted_before - res.deleted_after
        if res.lines is None:
            # In HTML, every resource is preceded by a blank line
            if is_html and new_body and new_body[-1] == "":
                new_body.pop()
            continue
        kept += 1
        if res.lines is token.diff:
            new_body.extend(token.raw)
        elif is_html:
            rows = [_html_row(l.rstrip(" \t\r\n")) for l in res.lines if l.rstrip(" \t\r\n")]
            new_body.extend([token.raw[0], '<div class="diff_container">', "<table>", "", *rows, "</table>", "</div>"])
        else:
            new_body.extend([token.raw[0], "```diff", *res.lines, "```"])

    if resources and kept == 0:
        if " -> " not in name:
            result.stats.apps_removed = 1
            return result
        new_body = ["", HTML_RENAMED_EMPTY, ""] if is_html else [MD_RENAMED_EMPTY, ""]

    result.lines = header + new_body
    return result


def _change_stats(added: int, deleted: int) -> str:
    if added > 0 and deleted > 0:
        return f" (+{added}|-{deleted})"
    if added > 0:
        return f" (+{added})"
    if deleted > 0:
        return f" (-{deleted})"
    return ""


def _update_summary(summary: str, sections: list[SectionResult]) -> str:
    lines = summary.split("\n")
    groups: dict[str, int] = {}
    modified: list[int] = []
    current = None
    for idx, line in enumerate(lines):
        m = SUMMARY_HEADER_RE.match(line)
        if m:
            current = m.group(1)
            groups[current] = idx
            continue
        if current == "Modified" and SUMMARY_ENTRY_RE.match(line) and line.startswith("± "):
            modified.append(idx)

    deleted_count = 0
    if "Deleted" in groups:
        deleted_count = int(SUMMARY_HEADER_RE.match(lines[groups["Deleted"]]).group(2))

    used: set[int] = set()
    remove: set[int] = set()
    for s_idx, section in enumerate(sections):
        if section.lines is not None and section.delta_added == 0 and section.delta_deleted == 0:
            continue
        target = None
        pos = s_idx - deleted_count
        if 0 <= pos < len(modified) and SUMMARY_ENTRY_RE.match(lines[modified[pos]]).group("name") == section.name:
            target = modified[pos]
        else:
            target = next(
                (i for i in modified if i not in used and SUMMARY_ENTRY_RE.match(lines[i]).group("name") == section.name),
                None,
            )
        if target is None:
            continue
        used.add(target)
        if section.lines is None:
            remove.add(target)
            continue
        m = SUMMARY_ENTRY_RE.match(lines[target])
        added = deleted = 0
        stats = m.group("stats") or ""
        for part in stats.split("|"):
            if part.startswith("+"):
                added = int(part[1:])
            elif part.startswith("-"):
                deleted = int(part[1:])
        added = max(0, added - section.delta_added)
        deleted = max(0, deleted - section.delta_deleted)
        lines[target] = f"± {m.group('name')}{_change_stats(added, deleted)}"

    if not remove:
        return "\n".join(lines)

    if "Modified" in groups:
        header_idx = groups["Modified"]
        count = int(SUMMARY_HEADER_RE.match(lines[header_idx]).group(2)) - len(remove)
        if count > 0:
            lines[header_idx] = f"Modified ({count}):"
        else:
            remove.add(header_idx)
            # Remove the blank line separating the group from the previous one
            if header_idx > 0 and lines[header_idx - 1] == "":
                remove.add(header_idx - 1)

    lines = [l for i, l in enumerate(lines) if i not in remove]
    while lines and lines[0] == "":
        lines.pop(0)
    return "\n".join(lines) if any(l.strip() for l in lines) else "No changes found"


def process_document(text: str, cfg: Config) -> tuple[str, Stats]:
    is_html = text.lstrip().startswith("<html")
    lines = text.split("\n")
    total = Stats()

    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if lines[i] == "<details>":
            j = i + 1
            while j < len(lines) and lines[j] != "</details>":
                j += 1
            if j == len(lines):
                break
            spans.append((i, j))
            i = j + 1
        else:
            i += 1
    if not spans:
        return text, total

    # Sections are separated by exactly one blank line; anything else is unexpected
    for (_, prev_end), (next_start, _) in zip(spans, spans[1:]):
        if any(l.strip() for l in lines[prev_end + 1 : next_start]):
            print("warning: unexpected content between applications, file left unchanged", file=sys.stderr)
            return text, total

    results: list[SectionResult] = []
    for start, end in spans:
        header, body = lines[start : start + 4], lines[start + 4 : end]
        if is_html:
            valid = header[1:2] == ["<summary>"] and header[3:4] == ["</summary>"]
            name_line = html.unescape(header[2]) if valid else ""
        else:
            m = re.match(r"^<summary>(.*)</summary>$", header[1] if len(header) > 1 else "")
            valid = m is not None and header[2:] == ["<br>", ""]
            name_line = m.group(1) if m else ""
        m = SECTION_NAME_RE.match(name_line)
        if not valid or not m:
            results.append(SectionResult(name_line, lines[start : end + 1]))
            continue
        res = _process_section(m.group("name"), header, body, is_html, cfg)
        if res.lines is not None:
            res.lines = res.lines + ["</details>"]
        total.add(res.stats)
        results.append(res)

    region: list[str] = []
    for res in results:
        if res.lines is None:
            continue
        if region:
            region.append("")
        region.extend(res.lines)
    if not region:
        region = ["No changes found"]

    new_lines = lines[: spans[0][0]] + region + lines[spans[-1][1] + 1 :]
    new_text = "\n".join(new_lines)

    if is_html:
        pattern = re.compile(r"(<p>Summary:</p>\n<pre>)(.*?)(</pre>)", re.DOTALL)
    else:
        pattern = re.compile(r"(Summary:\n```yaml\n)(.*?)(\n```)", re.DOTALL)
    new_text = pattern.sub(lambda m: m.group(1) + _update_summary(m.group(2), results) + m.group(3), new_text, count=1)
    return new_text, total


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Remove differences caused only by reordered list entries from argocd-diff-preview output (diff.md / diff.html).",
    )
    parser.add_argument("files", nargs="+", help="diff.md and/or diff.html files to clean (modified in place)")
    parser.add_argument(
        "--paths",
        default=DEFAULT_PATHS,
        help="Comma or newline separated JSONPaths of arrays whose element order is ignored, "
        f"e.g. '.spec.template.spec.containers[*].env' (default: {DEFAULT_PATHS})",
    )
    parser.add_argument(
        "--all-lists",
        action="store_true",
        help="Ignore the order of every list (careful: the order of args, command, initContainers, ... matters)",
    )
    parser.add_argument(
        "--line-count",
        type=int,
        default=_env_int("LINE_COUNT", 5),
        help="--line-count used by argocd-diff-preview (default: $LINE_COUNT or 5)",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=None,
        help="Context lines around changes in the cleaned output (default: same as --line-count)",
    )
    parser.add_argument(
        "--diff-ignore",
        default=os.environ.get("DIFF_IGNORE", ""),
        help="--diff-ignore regex used by argocd-diff-preview (default: $DIFF_IGNORE)",
    )
    parser.add_argument(
        "--keep-mixed-hunks",
        action="store_true",
        help="Only remove hunks that consist purely of reorderings; keep hunks with real changes unmodified",
    )
    parser.add_argument("-o", "--output", help="Write the result to this file instead of modifying the input (single input only)")
    args = parser.parse_args(argv)

    if args.output and len(args.files) != 1:
        parser.error("--output can only be used with a single input file")

    try:
        paths = parse_paths(args.paths)
    except ValueError as e:
        parser.error(f"--paths: {e}")

    ignore_regex = None
    if args.diff_ignore:
        try:
            ignore_regex = re.compile(args.diff_ignore)
        except re.error:
            ignore_regex = re.compile(re.escape(args.diff_ignore))

    cfg = Config(
        paths=paths,
        all_lists=args.all_lists,
        line_count=args.line_count,
        context=args.context if args.context is not None else args.line_count,
        keep_mixed_hunks=args.keep_mixed_hunks,
        ignore_regex=ignore_regex,
    )

    for path in args.files:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        new_text, stats = process_document(text, cfg)
        with open(args.output or path, "w", encoding="utf-8") as f:
            f.write(new_text)
        print(
            f"{path}: removed {stats.hunks_removed} reorder-only hunk(s), rewrote {stats.hunks_rewritten} hunk(s), "
            f"removed {stats.resources_removed} resource(s) and {stats.apps_removed} application(s)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
