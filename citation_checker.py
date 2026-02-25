#!/usr/bin/env python3
"""
Legal Brief Citation Checker

Parses a .docx legal brief, extracts case law citations, and verifies each
one against the CourtListener API to detect potentially fabricated cases.

Usage:
    python citation_checker.py brief.docx --token YOUR_TOKEN [--csv output.csv]

The token can also be set via the COURTLISTENER_TOKEN environment variable.
Get a free token at: https://www.courtlistener.com/sign-in/
"""

import argparse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass, field

import docx
import requests

try:
    import pymupdf  # PyMuPDF
except ImportError:
    pymupdf = None

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Citation:
    """A parsed case citation from the document."""
    full_text: str
    parties: str
    volume: str
    reporter: str
    page: str
    pin_cite: str = ""
    court: str = ""
    year: str = ""
    # Verification results
    status: str = "pending"  # verified, mismatch, not_found, unrecognized, error
    matched_case_name: str = ""
    detail: str = ""


# ---------------------------------------------------------------------------
# Citation extraction
# ---------------------------------------------------------------------------

# Reporter abbreviations grouped by type.
# Each entry is a regex fragment (periods escaped, optional spacing).

FEDERAL_SUPREME = [
    r"U\.S\.",
    r"S\.\s?Ct\.",
    r"L\.\s?Ed\.(?:\s?2d)?",
]

FEDERAL_CIRCUIT_DISTRICT = [
    r"F\.4th",
    r"F\.3d",
    r"F\.2d",
    r"F\.",
    r"F\.\s?Supp\.\s?3d",
    r"F\.\s?Supp\.\s?2d",
    r"F\.\s?Supp\.",
    r"F\.\s?App[\u2019']x",
    r"B\.R\.",
    r"Fed\.\s?Cl\.",
    r"M\.J\.",
    r"Vet\.\s?App\.",
]

REGIONAL_REPORTERS = [
    r"N\.E\.3d", r"N\.E\.2d", r"N\.E\.",
    r"N\.W\.2d", r"N\.W\.",
    r"S\.E\.2d", r"S\.E\.",
    r"S\.W\.3d", r"S\.W\.2d", r"S\.W\.",
    r"So\.\s?3d", r"So\.\s?2d", r"So\.",
    r"P\.3d", r"P\.2d", r"P\.",
    r"A\.3d", r"A\.2d", r"A\.",
]

STATE_REPORTERS = [
    r"Cal\.\s?Rptr\.\s?3d", r"Cal\.\s?Rptr\.\s?2d", r"Cal\.\s?Rptr\.",
    r"N\.Y\.S\.3d", r"N\.Y\.S\.2d", r"N\.Y\.S\.",
    r"Ill\.\s?Dec\.",
    r"Ill\.\s?2d",
    r"Wis\.\s?2d",
    r"Mich\.\s?App\.",
    r"Ohio\s?St\.\s?3d", r"Ohio\s?St\.\s?2d",
    r"Pa\.\s?Super\.",
    r"Wash\.\s?2d", r"Wash\.\s?App\.",
    r"Mass\.\s?App\.\s?Ct\.",
]

ALL_REPORTERS = (
    FEDERAL_SUPREME
    + FEDERAL_CIRCUIT_DISTRICT
    + REGIONAL_REPORTERS
    + STATE_REPORTERS
)

REPORTER_PATTERN = "(?:" + "|".join(ALL_REPORTERS) + ")"

# Full citation:  Party v. Party, Volume Reporter Page(, PinCite) (Court Year)
FULL_CITE_RE = re.compile(
    r"(?P<parties>"
    r"(?:In\s+re|Ex\s+[Pp]arte)?\s*"            # Optional "In re" / "Ex parte"
    r"[A-Z][A-Za-z0-9\u2019'.&,\-\s]+"           # First party
    r"\s+v\.?\s+"                                  # "v." separator
    r"[A-Z][A-Za-z0-9\u2019'.&,\-\s]+?"          # Second party
    r")"
    r",\s*"
    r"(?P<volume>\d{1,4})"
    r"\s+"
    r"(?P<reporter>" + REPORTER_PATTERN + r")"
    r"\s+"
    r"(?P<page>\d{1,5})"
    r"(?:,\s*(?P<pin_cite>\d{1,5}(?:\s*[-\u2013]\s*\d{1,5})?))??"
    r"\s*"
    r"\("
    r"(?P<court>[A-Za-z0-9.\s]*?)"
    r"(?P<year>\d{4})"
    r"\)",
    re.VERBOSE,
)

# "In re" style without "v." — e.g., In re Grand Jury Subpoena, 123 F.3d 456 (2d Cir. 2005)
IN_RE_CITE_RE = re.compile(
    r"(?P<parties>"
    r"(?:In\s+re|Ex\s+[Pp]arte)\s+"
    r"[A-Z][A-Za-z0-9\u2019'.&,\-\s]+?"
    r")"
    r",\s*"
    r"(?P<volume>\d{1,4})"
    r"\s+"
    r"(?P<reporter>" + REPORTER_PATTERN + r")"
    r"\s+"
    r"(?P<page>\d{1,5})"
    r"(?:,\s*(?P<pin_cite>\d{1,5}(?:\s*[-\u2013]\s*\d{1,5}))?)??"
    r"\s*"
    r"\("
    r"(?P<court>[A-Za-z0-9.\s]*?)"
    r"(?P<year>\d{4})"
    r"\)",
)


def extract_text_from_docx(filepath: str) -> str:
    """Extract all text from a .docx file, including footnotes and endnotes."""
    doc = docx.Document(filepath)
    parts = []

    # Main body paragraphs
    for para in doc.paragraphs:
        parts.append(para.text)

    # Footnotes (if accessible via the XML)
    try:
        footnotes_part = doc.part.package.part_related_by(
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
        )
        if footnotes_part is not None:
            from lxml import etree
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            tree = etree.fromstring(footnotes_part.blob)
            for fn in tree.findall(".//w:footnote", ns):
                fn_id = fn.get(f"{{{ns['w']}}}id")
                if fn_id in ("-1", "0"):  # Skip separator/continuation
                    continue
                texts = fn.findall(".//w:t", ns)
                parts.append(" ".join(t.text for t in texts if t.text))
    except Exception:
        pass  # Footnote extraction is best-effort

    # Endnotes
    try:
        endnotes_part = doc.part.package.part_related_by(
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes"
        )
        if endnotes_part is not None:
            from lxml import etree
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            tree = etree.fromstring(endnotes_part.blob)
            for en in tree.findall(".//w:endnote", ns):
                en_id = en.get(f"{{{ns['w']}}}id")
                if en_id in ("-1", "0"):
                    continue
                texts = en.findall(".//w:t", ns)
                parts.append(" ".join(t.text for t in texts if t.text))
    except Exception:
        pass

    return "\n".join(parts)


def extract_text_from_pdf(filepath: str) -> str:
    """Extract all text from a PDF file using PyMuPDF."""
    if pymupdf is None:
        raise RuntimeError(
            "PDF support requires PyMuPDF. Install it with: pip install pymupdf"
        )
    doc = pymupdf.open(filepath)
    parts = []
    for page in doc:
        parts.append(page.get_text())
    doc.close()
    return "\n".join(parts)


def extract_text(filepath: str) -> str:
    """Extract text from a .docx or .pdf file based on extension."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".docx":
        return extract_text_from_docx(filepath)
    elif ext == ".pdf":
        return extract_text_from_pdf(filepath)
    else:
        raise ValueError(f"Unsupported file type: {ext}. Use .docx or .pdf")


# Common legal abbreviations that end with "." but are NOT sentence boundaries
_LEGAL_ABBREVS = {
    "inc", "corp", "co", "ltd", "llc", "llp", "lp", "no", "nos",
    "assn", "ass'n", "assoc", "dept", "div", "dist", "gov", "govt",
    "elec", "indus", "mfg", "mgmt", "nat'l", "natl", "intl", "int'l",
    "ins", "grp", "sys", "tech", "servs", "svcs", "bros", "constr",
    "transp", "univ", "hosp", "pharm", "telecomm", "commc'ns",
    "st", "ave", "blvd", "dr", "jr", "sr", "mr", "mrs", "ms",
    "al",  # "et al."
}


def _trim_party_name(parties: str) -> str:
    """Trim excess preceding text from a captured party name string."""
    v_match = re.search(r"\s+v\.?\s+", parties)
    if not v_match:
        return parties

    before_v = parties[:v_match.start()]

    # Walk backwards through ". " boundaries, keeping legal abbreviations
    best_trim = None
    for m in re.finditer(r"(\w+)\.\s+", before_v):
        word_before_dot = m.group(1).lower().rstrip("'")
        # If it's a legal abbreviation, skip — it's part of the party name
        if word_before_dot in _LEGAL_ABBREVS:
            continue
        # If the word before the dot is very short (1-2 chars) and not a known
        # sentence-ender, assume it's an abbreviation
        if len(word_before_dot) <= 2 and word_before_dot not in {"id"}:
            continue
        # This looks like a real sentence boundary
        best_trim = m.end()

    # Also check for "; " boundaries (always sentence boundaries)
    for m in re.finditer(r";\s+", before_v):
        pos = m.end()
        if best_trim is None or pos > best_trim:
            best_trim = pos

    if best_trim is not None:
        parties = parties[best_trim:]

    return parties


def extract_citations(text: str) -> list[Citation]:
    """Extract all case law citations from the given text."""
    citations = []
    seen = set()

    for pattern in (FULL_CITE_RE, IN_RE_CITE_RE):
        for m in pattern.finditer(text):
            # Deduplicate by volume + reporter + page
            key = (m.group("volume"), m.group("reporter").strip(), m.group("page"))
            if key in seen:
                continue
            seen.add(key)

            parties = m.group("parties").strip()
            # Clean up extra whitespace in party names
            parties = re.sub(r"\s+", " ", parties)
            # Trim excess text before the actual case name.
            # The regex can greedily capture preceding sentence text.
            parties = _trim_party_name(parties)

            citations.append(Citation(
                full_text=m.group(0).strip(),
                parties=parties,
                volume=m.group("volume"),
                reporter=m.group("reporter").strip(),
                page=m.group("page"),
                pin_cite=m.group("pin_cite") or "",
                court=m.group("court").strip().rstrip(",. "),
                year=m.group("year"),
            ))

    return citations


# ---------------------------------------------------------------------------
# CourtListener API verification
# ---------------------------------------------------------------------------

API_BASE = "https://www.courtlistener.com/api/rest/v4"
CITATION_LOOKUP_URL = f"{API_BASE}/citation-lookup/"
SEARCH_URL = f"{API_BASE}/search/"

# Throttle: stay under 60 citations per minute
REQUEST_DELAY = 1.1  # seconds between requests


def verify_citation(citation: Citation, session: requests.Session) -> Citation:
    """Verify a single citation against the CourtListener API."""

    # --- Step 1: Citation lookup by volume/reporter/page ---
    try:
        resp = session.post(
            CITATION_LOOKUP_URL,
            data={
                "text": f"{citation.volume} {citation.reporter} {citation.page}",
            },
            timeout=30,
        )
    except requests.RequestException as e:
        citation.status = "error"
        citation.detail = f"API request failed: {e}"
        return citation

    if resp.status_code == 429:
        citation.status = "error"
        citation.detail = "Rate limited — try again later"
        return citation

    if resp.status_code not in (200, 300):
        # Try parsing the JSON response for per-citation statuses
        pass

    # The citation-lookup endpoint returns a list of citation results
    try:
        data = resp.json()
    except ValueError:
        citation.status = "error"
        citation.detail = f"Invalid JSON response (HTTP {resp.status_code})"
        return citation

    # data is a list of citation result objects
    if not data:
        citation.status = "not_found"
        citation.detail = "No results from citation lookup"
        return citation

    # Find the result matching our citation
    matched_result = None
    for result in data:
        if isinstance(result, dict):
            status_code = result.get("status")
            if status_code == 200:
                matched_result = result
                break
            elif status_code == 300:
                matched_result = result
                break
            elif status_code == 404:
                citation.status = "not_found"
                citation.detail = "Citation not found in CourtListener database"
                return citation
            elif status_code == 400:
                citation.status = "unrecognized"
                citation.detail = result.get("error_message", "Unrecognized reporter")
                return citation

    if matched_result is None:
        # If the response is a single object (not a list)
        if isinstance(data, dict):
            clusters = data.get("clusters", [])
            if clusters:
                matched_result = data
            else:
                citation.status = "not_found"
                citation.detail = "No matching clusters found"
                return citation
        else:
            citation.status = "not_found"
            citation.detail = "No valid match in API response"
            return citation

    # Extract the matched case name from clusters
    clusters = matched_result.get("clusters", [])
    if clusters:
        cluster = clusters[0]
        case_name = cluster.get("caseName", "") or cluster.get("case_name", "")
        citation.matched_case_name = case_name

        # Compare case names for mismatch detection
        if case_name and not _names_match(citation.parties, case_name):
            citation.status = "mismatch"
            citation.detail = f"Citation exists but name differs: \"{case_name}\""
        else:
            citation.status = "verified"
            citation.detail = f"Matches: \"{case_name}\""
    else:
        citation.status = "verified"
        citation.detail = "Citation found (no case name to cross-check)"

    return citation


def _names_match(cited_parties: str, db_case_name: str) -> bool:
    """
    Check if the cited party names reasonably match the database case name.
    Uses a fuzzy approach: extracts key words and checks for overlap.
    """
    def normalize(name: str) -> set[str]:
        name = name.lower()
        # Remove common filler words and punctuation
        name = re.sub(r"[.,;:'\"\u2019()\[\]]", " ", name)
        stopwords = {
            "v", "vs", "the", "of", "in", "re", "ex", "parte", "et", "al",
            "a", "an", "and", "for", "on", "by", "no", "inc", "corp",
            "co", "ltd", "llc", "city", "state", "united", "states",
            "county", "board", "dept", "department",
        }
        words = set(name.split()) - stopwords
        # Remove very short words
        words = {w for w in words if len(w) > 1}
        return words

    cited_words = normalize(cited_parties)
    db_words = normalize(db_case_name)

    if not cited_words or not db_words:
        return True  # Can't compare, assume match

    overlap = cited_words & db_words
    # Consider it a match if at least 40% of the cited words appear in the DB name
    # or at least 40% of DB words appear in cited name
    ratio_cited = len(overlap) / len(cited_words) if cited_words else 0
    ratio_db = len(overlap) / len(db_words) if db_words else 0

    return ratio_cited >= 0.4 or ratio_db >= 0.4


def verify_all_citations(
    citations: list[Citation], token: str, verbose: bool = True
) -> list[Citation]:
    """Verify all citations against the CourtListener API."""
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Token {token}",
    })

    total = len(citations)
    for i, cite in enumerate(citations, 1):
        if verbose:
            print(f"  [{i}/{total}] Checking: {cite.volume} {cite.reporter} {cite.page} ...", end=" ", flush=True)

        verify_citation(cite, session)

        if verbose:
            status_str = _status_label(cite.status)
            print(status_str)

        # Rate limiting
        if i < total:
            time.sleep(REQUEST_DELAY)

    return citations


# ---------------------------------------------------------------------------
# Output / reporting
# ---------------------------------------------------------------------------

# ANSI color codes
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
GRAY = "\033[90m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _status_label(status: str) -> str:
    labels = {
        "verified": f"{GREEN}VERIFIED{RESET}",
        "mismatch": f"{YELLOW}NAME MISMATCH{RESET}",
        "not_found": f"{RED}NOT FOUND{RESET}",
        "unrecognized": f"{GRAY}UNRECOGNIZED REPORTER{RESET}",
        "error": f"{RED}ERROR{RESET}",
        "pending": f"{GRAY}PENDING{RESET}",
    }
    return labels.get(status, status)


def print_report(citations: list[Citation]) -> None:
    """Print a detailed report to the console."""
    print(f"\n{'=' * 70}")
    print(f"{BOLD}  CITATION VERIFICATION REPORT{RESET}")
    print(f"{'=' * 70}\n")

    if not citations:
        print("  No case citations found in the document.\n")
        return

    # Group by status for readability
    groups = {
        "not_found": [],
        "mismatch": [],
        "unrecognized": [],
        "error": [],
        "verified": [],
    }
    for cite in citations:
        groups.get(cite.status, groups["error"]).append(cite)

    # Print problematic citations first
    for status_key in ("not_found", "mismatch", "unrecognized", "error", "verified"):
        group = groups[status_key]
        if not group:
            continue

        print(f"  {_status_label(status_key)} ({len(group)})")
        print(f"  {'-' * 50}")
        for cite in group:
            print(f"    {cite.parties}")
            print(f"    {cite.volume} {cite.reporter} {cite.page} ({cite.court} {cite.year})")
            if cite.detail:
                print(f"    -> {cite.detail}")
            print()

    # Summary
    total = len(citations)
    verified = len(groups["verified"])
    not_found = len(groups["not_found"])
    mismatch = len(groups["mismatch"])
    unrecognized = len(groups["unrecognized"])
    errors = len(groups["error"])

    print(f"{'=' * 70}")
    print(f"  SUMMARY: {total} citations checked")
    print(f"    {GREEN}Verified:     {verified}{RESET}")
    print(f"    {RED}Not found:    {not_found}{RESET}")
    print(f"    {YELLOW}Mismatched:   {mismatch}{RESET}")
    print(f"    {GRAY}Unrecognized: {unrecognized}{RESET}")
    if errors:
        print(f"    {RED}Errors:       {errors}{RESET}")
    print(f"{'=' * 70}\n")

    if not_found > 0 or mismatch > 0:
        print(f"  {RED}{BOLD}WARNING: {not_found + mismatch} citation(s) could not be verified.{RESET}")
        print(f"  These may be AI-generated / hallucinated case citations.\n")


def write_csv(citations: list[Citation], filepath: str) -> None:
    """Write verification results to a CSV file."""
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Citation", "Parties", "Volume", "Reporter", "Page",
            "Court", "Year", "Status", "Matched Case Name", "Detail",
        ])
        for cite in citations:
            writer.writerow([
                f"{cite.volume} {cite.reporter} {cite.page}",
                cite.parties,
                cite.volume,
                cite.reporter,
                cite.page,
                cite.court,
                cite.year,
                cite.status,
                cite.matched_case_name,
                cite.detail,
            ])
    print(f"  Results saved to: {filepath}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Check a legal brief (.docx or .pdf) for potentially fabricated case citations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python citation_checker.py brief.docx --token abc123\n"
            "  python citation_checker.py brief.docx --csv results.csv\n"
            "\n"
            "Get a free CourtListener API token at:\n"
            "  https://www.courtlistener.com/sign-in/"
        ),
    )
    parser.add_argument("docx_file", help="Path to the .docx legal brief")
    parser.add_argument(
        "--token",
        default=os.environ.get("COURTLISTENER_TOKEN", ""),
        help="CourtListener API token (or set COURTLISTENER_TOKEN env var)",
    )
    parser.add_argument(
        "--csv",
        dest="csv_file",
        default="",
        help="Optional path to export results as CSV",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only extract and list citations without verifying them",
    )

    args = parser.parse_args()

    # Validate inputs
    if not os.path.isfile(args.docx_file):
        print(f"Error: File not found: {args.docx_file}", file=sys.stderr)
        sys.exit(1)

    if not args.docx_file.lower().endswith(".docx"):
        print(f"Error: File must be a .docx document: {args.docx_file}", file=sys.stderr)
        sys.exit(1)

    if not args.token and not args.list_only:
        print(
            "Error: A CourtListener API token is required for verification.\n"
            "  Provide it with --token or set the COURTLISTENER_TOKEN env var.\n"
            "  Get a free token at: https://www.courtlistener.com/sign-in/\n"
            "  Or use --list-only to just extract citations without verification.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Step 1: Extract text
    print(f"\n  Reading: {args.docx_file}")
    text = extract_text_from_docx(args.docx_file)
    print(f"  Extracted {len(text):,} characters of text.\n")

    # Step 2: Parse citations
    print("  Extracting case citations...")
    citations = extract_citations(text)
    print(f"  Found {len(citations)} unique case citation(s).\n")

    if not citations:
        print("  No case citations found. Nothing to verify.\n")
        sys.exit(0)

    # List the extracted citations
    print(f"  {'─' * 50}")
    for i, cite in enumerate(citations, 1):
        print(f"  {i:3}. {cite.parties}")
        print(f"       {cite.volume} {cite.reporter} {cite.page} ({cite.court} {cite.year})")
    print(f"  {'─' * 50}\n")

    if args.list_only:
        print("  (--list-only mode: skipping verification)\n")
        sys.exit(0)

    # Step 3: Verify
    print("  Verifying citations against CourtListener...\n")
    verify_all_citations(citations, args.token, verbose=True)

    # Step 4: Report
    print_report(citations)

    if args.csv_file:
        write_csv(citations, args.csv_file)


if __name__ == "__main__":
    main()
