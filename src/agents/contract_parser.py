"""Deterministic, lossless requirement/contract extraction for evidence v3."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from src.models.evidence import RequirementItem, SourceSpan


_HEADING_RE = re.compile(
    r"(?im)^\s{0,3}(?:#{1,6}\s*)?(requirements|new interfaces introduced)\s*:\s*$"
)
_BULLET_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?P<marker>[-*+] |\d+[.)] )(?P<body>\S.*)$"
)
_FIELD_RE = re.compile(
    r"(?im)^\s*(?:(?:[-*+]|\d+[.)])\s*)?"
    r"(Type|Name|Path|Input|Output|Description)\s*:\s*(.+?)\s*$"
)
_PATH_RE = re.compile(
    r"(?<![\w.-])(?:\.?\.?[/\\])?[\w.-]+(?:[/\\][\w.@+-]+)+\.[A-Za-z0-9]+"
)
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_PARAGRAPH_RE = re.compile(r"(?ms)(?P<body>\S.*?)(?=\n[ \t]*\n|\Z)")


@dataclass(frozen=True)
class ContractBlock:
    start: int
    end: int
    text: str
    origin: str
    kind: str


def _logical_newline_view(text: str) -> str:
    """Decode literal ``\\n`` delimiters without changing source offsets."""
    # Keep two characters (newline + padding space) for every escaped newline,
    # so regex spans in the logical view still slice the exact original text.
    return text.replace("\\n", "\n ")


def _logical_line_content_start(section: str, match: re.Match[str]) -> int:
    """Skip offset-preserving padding while retaining bullets/numbers."""
    matched = section[match.start():match.end()]
    return match.start() + (len(matched) - len(matched.lstrip()))


def _is_group_heading_paragraph(raw: str) -> bool:
    """Return True for non-actionable paragraph headers such as `Foo:`."""
    stripped = raw.strip()
    if not stripped or not stripped.endswith(":"):
        return False
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if len(lines) != 1:
        return False
    if _FIELD_RE.match(stripped):
        return False
    return True


def _section_ranges(text: str) -> list[tuple[str, int, int]]:
    matches = list(_HEADING_RE.finditer(text))
    result: list[tuple[str, int, int]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        origin = "requirements" if match.group(1).lower() == "requirements" else "new_interfaces"
        result.append((origin, match.end(), end))
    return result


def extract_contract_blocks(text: str) -> list[ContractBlock]:
    """Return top-level behavior bullets and whole interface blocks.

    Nested bullets and interface fields remain attributes of their parent
    contract, preventing field inflation while retaining the exact source.
    """
    blocks: list[ContractBlock] = []
    view = _logical_newline_view(text)
    for origin, section_start, section_end in _section_ranges(view):
        section = view[section_start:section_end]
        fields = list(_FIELD_RE.finditer(section))

        # Interface sections commonly use an unbulleted ``Type: ...`` line
        # followed by several top-level ``- Name:``, ``- Path:``, ``- Fields:``
        # and ``- Description:`` bullets.  Those bullets are attributes of one
        # contract, not independent repair requirements.  Type boundaries are
        # therefore authoritative whenever they are present, even if bullets
        # also exist in the section.
        if origin == "new_interfaces":
            type_starts = [m for m in fields if m.group(1).lower() == "type"]
            if type_starts:
                for idx, starter in enumerate(type_starts):
                    logical_start = _logical_line_content_start(section, starter)
                    start = section_start + logical_start
                    end = section_start + (
                        _logical_line_content_start(section, type_starts[idx + 1])
                        if idx + 1 < len(type_starts)
                        else len(section)
                    )
                    raw = text[start:end].strip()
                    actual_start = text.find(raw, start, end)
                    blocks.append(
                        ContractBlock(
                            actual_start,
                            actual_start + len(raw),
                            raw,
                            origin,
                            "interface",
                        )
                    )
                continue

        bullets = list(_BULLET_RE.finditer(section))
        top_indent = min((len(m.group("indent").expandtabs(4)) for m in bullets), default=0)
        tops = [m for m in bullets if len(m.group("indent").expandtabs(4)) == top_indent]
        if tops:
            for idx, bullet in enumerate(tops):
                start = section_start + bullet.start("marker")
                end = section_start + (
                    tops[idx + 1].start("marker")
                    if idx + 1 < len(tops)
                    else len(section)
                )
                raw = text[start:end].rstrip()
                end = start + len(raw)
                kind = "interface" if origin == "new_interfaces" else "behavior"
                blocks.append(ContractBlock(start, end, raw, origin, kind))
            continue

        if origin == "requirements":
            paragraphs = list(_PARAGRAPH_RE.finditer(section))
            if paragraphs:
                for idx, paragraph in enumerate(paragraphs):
                    start = section_start + paragraph.start("body")
                    raw = text[start:section_start + paragraph.end("body")].rstrip()
                    end = start + len(raw)
                    if _is_group_heading_paragraph(raw) and idx + 1 < len(paragraphs):
                        continue
                    blocks.append(ContractBlock(start, end, raw, origin, "behavior"))
                continue

        # Some issue artifacts express an interface as an unbulleted field block.
        if fields:
            type_starts = [m for m in fields if m.group(1).lower() == "type"]
            starts = type_starts or [fields[0]]
            for idx, starter in enumerate(starts):
                start = section_start + _logical_line_content_start(section, starter)
                end = section_start + (
                    _logical_line_content_start(section, starts[idx + 1])
                    if idx + 1 < len(starts)
                    else len(section)
                )
                raw = text[start:end].strip()
                actual_start = text.find(raw, start, end)
                blocks.append(ContractBlock(actual_start, actual_start + len(raw), raw, origin, "interface"))
    return blocks


def _paths(raw: str) -> list[str]:
    return list(dict.fromkeys(p.replace("\\", "/") for p in _PATH_RE.findall(raw)))


def _symbols(raw: str) -> list[str]:
    values = list(_BACKTICK_RE.findall(raw))
    for key, value in _FIELD_RE.findall(_logical_newline_view(raw)):
        if key.lower() in {"name", "type"}:
            values.append(value.strip())
    return list(dict.fromkeys(v for v in values if len(v) <= 200))


def build_requirement_ledger(text: str) -> list[RequirementItem]:
    items: list[RequirementItem] = []
    for index, block in enumerate(extract_contract_blocks(text), 1):
        req_id = f"req-{index:03d}"
        contract_id = f"contract-{index:03d}"
        items.append(
            RequirementItem(
                id=req_id,
                text=block.text,
                origin=block.origin,
                source_span=SourceSpan(start=block.start, end=block.end, text=block.text),
                parent_contract_id=contract_id,
                contract_kind=block.kind,
                explicit_paths=_paths(block.text),
                explicit_symbols=_symbols(block.text),
                source_block_hash=hashlib.sha256(block.text.encode("utf-8")).hexdigest(),
            )
        )
    return items


def validate_ledger_coverage(text: str, items: list[RequirementItem]) -> None:
    """Mechanically reject dropped, altered, overlapping or detached blocks."""
    expected = extract_contract_blocks(text)
    spans = {(i.source_span.start, i.source_span.end): i for i in items if i.source_span}
    for block in expected:
        item = spans.get((block.start, block.end))
        if item is None:
            raise ValueError(f"unmapped input contract at source span {block.start}:{block.end}")
        if item.source_span.text != block.text or text[block.start:block.end] != block.text:
            raise ValueError(f"source text drift at {block.start}:{block.end}")
        digest = hashlib.sha256(block.text.encode("utf-8")).hexdigest()
        if item.source_block_hash != digest:
            raise ValueError(f"source hash mismatch for {item.id}")
