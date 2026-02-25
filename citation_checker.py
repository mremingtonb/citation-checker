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
from statistics import mean, stdev

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
    suggestion: str = ""  # "Did you mean?" suggested correct citation


@dataclass
class Quote:
    """A quoted passage attributed to a cited case."""
    text: str                  # The quoted text
    cite_index: int            # Index into citations list
    cite_label: str            # e.g., "123 So. 2d 456"
    status: str = "pending"    # pending / verified / not_in_case / found_elsewhere / not_found
    found_in: str = ""         # Case name where actually found (if misattributed)
    found_cite: str = ""       # Raw citation string where quote was actually found
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

# Simple regex to parse citation strings returned by CourtListener search
# e.g., "123 So. 2d 456" → volume=123, reporter="So. 2d", page=456
CITE_STRING_RE = re.compile(
    r"(\d{1,4})\s+(" + REPORTER_PATTERN + r")\s+(\d{1,5})"
)


def _edit_distance(s1: str, s2: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(s1) < len(s2):
        return _edit_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)

    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev_row[j + 1] + 1
            deletions = curr_row[j] + 1
            substitutions = prev_row[j] + (c1 != c2)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row
    return prev_row[-1]


def _normalize_reporter(r: str) -> str:
    """Normalize a reporter abbreviation for comparison.
    Strips spaces around periods: 'So. 2d' → 'So.2d'
    """
    return re.sub(r"\s+", "", r).lower()


# ---------------------------------------------------------------------------
# Reporter → jurisdiction mapping for jurisdiction-aware quote searching
# ---------------------------------------------------------------------------

# Maps normalized reporter abbreviations to CourtListener court filter strings.
# CourtListener search supports a "court" parameter for filtering by jurisdiction.
REPORTER_JURISDICTION = {
    # Florida
    "so.": "fla", "so.2d": "fla", "so.3d": "fla",
    # New York
    "n.y.s.": "ny", "n.y.s.2d": "ny", "n.y.s.3d": "ny",
    # California
    "cal.rptr.": "ca", "cal.rptr.2d": "ca", "cal.rptr.3d": "ca",
    # Northeast
    "n.e.": "northeast", "n.e.2d": "northeast", "n.e.3d": "northeast",
    # Northwest
    "n.w.": "northwest", "n.w.2d": "northwest",
    # Southeast
    "s.e.": "southeast", "s.e.2d": "southeast",
    # Southwest
    "s.w.": "southwest", "s.w.2d": "southwest", "s.w.3d": "southwest",
    # Pacific
    "p.": "pacific", "p.2d": "pacific", "p.3d": "pacific",
    # Atlantic
    "a.": "atlantic", "a.2d": "atlantic", "a.3d": "atlantic",
    # Illinois
    "ill.dec.": "il", "ill.2d": "il",
    # Ohio
    "ohiost.2d": "oh", "ohiost.3d": "oh",
    # Pennsylvania
    "pa.super.": "pa",
    # Washington
    "wash.2d": "wa", "wash.app.": "wa",
    # Wisconsin
    "wis.2d": "wi",
    # Michigan
    "mich.app.": "mi",
    # Massachusetts
    "mass.app.ct.": "ma",
}

# CourtListener court IDs for state jurisdictions
# Used to build the "court" parameter for CourtListener search API
JURISDICTION_COURTS = {
    "fla": "flaapp fla fladistctapp flasupct",
    "ny": "nyappdiv nyappterm ny nysupct",
    "ca": "calctapp cal",
    "il": "illappct ill",
    "oh": "ohioctapp ohio",
    "pa": "pasuperct pa",
    "wa": "washctapp wash",
    "wi": "wisctapp wis",
    "mi": "michctapp mich",
    "ma": "massappct mass",
    # Regional reporters cover multiple states — use broader search
    "northeast": "",
    "northwest": "",
    "southeast": "",
    "southwest": "",
    "pacific": "",
    "atlantic": "",
}


def _get_jurisdiction_courts(reporter: str) -> str:
    """Return CourtListener court filter string based on the reporter.

    For state-specific reporters (e.g., So. 2d → Florida), returns the
    court IDs to filter search results to that jurisdiction.
    Returns empty string if no specific jurisdiction can be determined.
    """
    norm = _normalize_reporter(reporter)
    jurisdiction = REPORTER_JURISDICTION.get(norm, "")
    if not jurisdiction:
        return ""
    return JURISDICTION_COURTS.get(jurisdiction, "")


def _citations_are_similar(
    cite_label: str, found_cite_str: str
) -> bool:
    """Check if two citation strings are similar enough to indicate human error.

    Compares the reporter (must match) and volume/page numbers (must be
    close by edit distance — like a typo).

    Examples:
        "123 So. 2d 456" vs "213 So. 2d 456" → True  (transposed digits)
        "123 So. 2d 456" vs "987 P.3d 777"   → False  (different reporter)
    """
    m_given = CITE_STRING_RE.search(cite_label)
    m_found = CITE_STRING_RE.search(found_cite_str)

    if not m_given or not m_found:
        return False

    given_vol, given_rptr, given_page = m_given.group(1), m_given.group(2), m_given.group(3)
    found_vol, found_rptr, found_page = m_found.group(1), m_found.group(2), m_found.group(3)

    # Reporter must match (same reporter family)
    if _normalize_reporter(given_rptr) != _normalize_reporter(found_rptr):
        return False

    # If volume AND page are identical, the citations are the same (not an error)
    if given_vol == found_vol and given_page == found_page:
        return True

    # Volume and page must be close (edit distance ≤ 3 combined)
    vol_dist = _edit_distance(given_vol, found_vol)
    page_dist = _edit_distance(given_page, found_page)
    total_dist = vol_dist + page_dist

    return total_dist <= 3


def _suggest_correction(
    citation: Citation, session: requests.Session
) -> str | None:
    """Search CourtListener by case name to suggest a corrected citation.

    When a citation is not found, this searches by party names to find the
    actual case, then checks if any of its real citations are close to the
    mistyped one (same reporter, edit distance ≤ 2 on volume/page).
    Returns the suggested citation string, or None.
    """
    if not citation.parties or len(citation.parties.strip()) < 3:
        return None

    # Rate-limit before the extra API call
    time.sleep(REQUEST_DELAY)

    # Build search query from party names
    parties_clean = re.sub(r"[^\w\s]", " ", citation.parties).strip()
    # Truncate long names
    if len(parties_clean) > 80:
        parties_clean = parties_clean[:80]

    try:
        resp = session.get(
            SEARCH_URL,
            params={"q": f'caseName:("{parties_clean}")', "type": "o"},
            timeout=30,
        )
    except requests.RequestException:
        return None

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
    except ValueError:
        return None

    results = data.get("results", [])
    if not results:
        return None

    our_reporter_norm = _normalize_reporter(citation.reporter)
    best_suggestion = None
    best_distance = 999

    for result in results[:10]:  # Check top 10 results
        cite_strings = result.get("citation", [])
        if not cite_strings:
            continue
        for cite_str in cite_strings:
            m = CITE_STRING_RE.search(cite_str)
            if not m:
                continue
            vol, rptr, page = m.group(1), m.group(2), m.group(3)
            if _normalize_reporter(rptr) != our_reporter_norm:
                continue
            # Same reporter — check if volume/page are close
            vol_dist = _edit_distance(citation.volume, vol)
            page_dist = _edit_distance(citation.page, page)
            total_dist = vol_dist + page_dist
            # Must be different but close (edit distance 1-3 on combined vol+page)
            if 0 < total_dist <= 3 and total_dist < best_distance:
                best_distance = total_dist
                best_suggestion = f"{vol} {rptr} {page}"

    return best_suggestion


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

    # --- Step 2: Parse response and determine status ---
    # data is a list of citation result objects
    if not data:
        citation.status = "not_found"
        citation.detail = "No results from citation lookup"
    else:
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
                elif status_code == 400:
                    citation.status = "unrecognized"
                    citation.detail = result.get("error_message", "Unrecognized reporter")
                    return citation

        if matched_result is None and citation.status == "pending":
            # If the response is a single object (not a list)
            if isinstance(data, dict):
                clusters = data.get("clusters", [])
                if clusters:
                    matched_result = data
                else:
                    citation.status = "not_found"
                    citation.detail = "No matching clusters found"
            else:
                citation.status = "not_found"
                citation.detail = "No valid match in API response"

        # Extract the matched case name from clusters
        if matched_result is not None:
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

    # --- Step 3: "Did you mean?" suggestion for not-found citations ---
    if citation.status == "not_found":
        suggestion = _suggest_correction(citation, session)
        if suggestion:
            citation.suggestion = suggestion
            citation.detail += f' Did you mean: {suggestion}?'

    # --- Step 4: For mismatches, search by the DB case name to find its
    #     correct citation and check if it's similar to what was given ---
    if citation.status == "mismatch" and citation.matched_case_name:
        suggestion = _suggest_correction(citation, session)
        if suggestion:
            citation.suggestion = suggestion
            citation.detail += f' Correct citation may be: {suggestion}.'

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
# AI-generation probability scoring (100-point scale)
# ---------------------------------------------------------------------------

def _split_sentences(text: str) -> list[str]:
    """Split text into sentences, respecting legal abbreviations."""
    _DOT = "<<DOT>>"  # safe placeholder that won't appear in legal text
    temp = text
    # Protect abbreviation periods with placeholder
    for abbrev in _LEGAL_ABBREVS:
        pat = re.compile(r'\b' + re.escape(abbrev) + r'\.', re.IGNORECASE)
        temp = pat.sub(lambda m: m.group(0)[:-1] + _DOT, temp)
    # Protect other common patterns
    temp = re.sub(r'([A-Z])\.([A-Z])', lambda m: m.group(1) + _DOT + m.group(2), temp)
    temp = re.sub(r'\b(Dr|Mr|Mrs|Ms|Jr|Sr|Prof|Hon|Rev)\.', lambda m: m.group(1) + _DOT, temp)
    temp = re.sub(r'\b(No|Nos|Vol|App|Supp|Cir|Dist|Ct)\.', lambda m: m.group(1) + _DOT, temp)
    temp = re.sub(r'\b(v|vs)\.', lambda m: m.group(1) + _DOT, temp)
    temp = re.sub(r'\b(e\.g|i\.e|cf|et al)\.', lambda m: m.group(0)[:-1] + _DOT, temp)
    temp = re.sub(r'\b(Id|id)\.', lambda m: m.group(1) + _DOT, temp)
    temp = re.sub(r'(\d)(st|nd|rd|th)\.', lambda m: m.group(0)[:-1] + _DOT, temp)

    sentences = re.split(r'(?<=[.!?])\s+', temp)
    sentences = [s.replace(_DOT, '.') for s in sentences]
    sentences = [s.strip() for s in sentences if len(s.strip()) > 15]
    return sentences


# --- Criterion 3: Improper citation formatting (max 5 pts) ---

_MALFORMED_CITE_PATTERNS = [
    re.compile(r'\d{1,4}\s+F\d[a-z]*\s+\d{1,5}'),             # "123 F3d 456"
    re.compile(r'\d{1,4}\s+S\s?Ct\b\s+\d{1,5}'),              # "583 SCt 2459"
    re.compile(r'\d{1,4}\s+L\s?Ed\b\s+\d{1,5}'),              # "576 LEd 123"
    re.compile(r'\d{1,4}\s+US\s+\d{1,5}'),                     # "547 US 410"
    re.compile(r'\d{1,4}\s+FSupp\s+\d{1,5}'),                  # "123 FSupp 456"
]


def _detect_formatting_issues(text: str) -> dict:
    """Criterion 3: Detect improperly formatted citations (max 5 pts)."""
    count = 0
    details = []
    for pat in _MALFORMED_CITE_PATTERNS:
        matches = pat.findall(text)
        count += len(matches)
        for m in matches[:2]:
            details.append(m.strip())

    if count == 0:
        pts = 0
    elif count <= 2:
        pts = 2
    elif count <= 4:
        pts = 3
    else:
        pts = 5

    return {
        "points": pts,
        "max": 5,
        "detail": f"Found {count} malformed citation(s)" if count else "No formatting issues detected",
    }


# --- Criterion 4: Pro se litigant + complex legalese (max 20 pts) ---

_PRO_SE_PATTERNS = [
    re.compile(r'\bpro\s+se\b', re.IGNORECASE),
    re.compile(r'\bself[- ]represented\b', re.IGNORECASE),
    re.compile(r'\bwithout\s+(?:an?\s+)?attorney\b', re.IGNORECASE),
    re.compile(r'\bunrepresented\b', re.IGNORECASE),
]

_LATIN_LEGAL_PHRASES = [
    "inter alia", "sua sponte", "res judicata", "stare decisis",
    "prima facie", "arguendo", "sub judice", "amicus curiae",
    "de novo", "ab initio", "in limine", "nunc pro tunc",
    "pro hac vice", "quo warranto", "certiorari", "mandamus",
    "habeas corpus", "res ipsa loquitur", "voir dire", "in camera",
    "ipso facto", "per curiam", "sine qua non", "in personam",
    "in rem", "pendente lite", "ejusdem generis", "noscitur a sociis",
    "expressio unius",
]

_COMPLEX_LEGAL_TERMS = [
    "notwithstanding", "aforementioned", "hereinafter", "heretofore",
    "inasmuch as", "insofar as", "therein", "thereof", "whereby",
    "wherein", "wherefore", "theretofore", "hereinabove",
    "hereinbelow", "aforestated", "abovementioned",
]


def _detect_pro_se_legalese(text: str, pro_se_override: bool = False) -> dict:
    """Criterion 4: Pro se litigant using complex legalese (max 15 pts)."""
    auto_detected = any(p.search(text) for p in _PRO_SE_PATTERNS)
    is_pro_se = pro_se_override or auto_detected

    if not is_pro_se:
        return {
            "points": 0,
            "max": 15,
            "detail": "Pro se status not detected \u2014 criterion skipped",
            "pro_se_detected": False,
        }

    text_lower = text.lower()
    word_count = len(text.split())
    if word_count < 100:
        return {
            "points": 0,
            "max": 15,
            "detail": "Pro se detected but document too short to analyze",
            "pro_se_detected": True,
        }

    latin_count = sum(1 for phrase in _LATIN_LEGAL_PHRASES if phrase in text_lower)
    complex_count = sum(
        len(re.findall(r'\b' + re.escape(term) + r'\b', text_lower))
        for term in _COMPLEX_LEGAL_TERMS
    )

    legalese_per_1k = ((latin_count + complex_count) / word_count) * 1000

    if legalese_per_1k < 1:
        pts = 0
    elif legalese_per_1k < 2:
        pts = 4
    elif legalese_per_1k < 3.5:
        pts = 8
    elif legalese_per_1k < 5:
        pts = 12
    else:
        pts = 15

    source = "user-indicated" if pro_se_override else "auto-detected"
    return {
        "points": pts,
        "max": 15,
        "detail": (
            f"Pro se brief ({source}) with {latin_count} Latin phrase(s) and "
            f"{complex_count} complex legal term(s) "
            f"({legalese_per_1k:.1f} per 1,000 words)"
        ),
        "pro_se_detected": True,
    }


# --- Criterion 5: Unusual syntax (max 10 pts) ---

def _detect_unusual_syntax(text: str) -> dict:
    """Criterion 5: Detect unusual syntax patterns (max 10 pts)."""
    sentences = _split_sentences(text)
    if len(sentences) < 5:
        return {"points": 0, "max": 10, "detail": "Not enough text to analyze syntax"}

    findings = []
    pts = 0

    # 5a: Sentence length uniformity
    lengths = [len(s.split()) for s in sentences]
    avg_len = mean(lengths)
    if avg_len > 0 and len(lengths) >= 5:
        sd = stdev(lengths)
        cv = sd / avg_len
        if cv < 0.25:
            pts += 4
            findings.append(f"Very uniform sentence lengths (CV={cv:.2f})")
        elif cv < 0.35:
            pts += 2
            findings.append(f"Somewhat uniform sentence lengths (CV={cv:.2f})")

    # 5b: Passive voice density
    passive_patterns = [
        re.compile(r'\b(?:is|are|was|were|been|being)\s+\w+ed\b'),
        re.compile(r'\b(?:is|are|was|were|been|being)\s+\w+en\b'),
    ]
    passive_count = sum(len(p.findall(text)) for p in passive_patterns)
    passive_ratio = passive_count / len(sentences) if sentences else 0
    if passive_ratio > 0.6:
        pts += 4
        findings.append(f"High passive voice density ({passive_ratio:.0%} of sentences)")
    elif passive_ratio > 0.4:
        pts += 2
        findings.append(f"Elevated passive voice ({passive_ratio:.0%} of sentences)")

    # 5c: Overly long average sentence length
    if avg_len > 35:
        pts += 2
        findings.append(f"Very long average sentences ({avg_len:.0f} words)")

    return {
        "points": min(pts, 10),
        "max": 10,
        "detail": "; ".join(findings) if findings else "No unusual syntax patterns detected",
    }


# --- Criterion 6: Out-of-jurisdiction citations (max 10 pts) ---

_CIRCUIT_STATES = {
    "1": {"me", "ma", "nh", "ri", "pr"},
    "2": {"ct", "ny", "vt"},
    "3": {"de", "nj", "pa", "vi"},
    "4": {"md", "nc", "sc", "va", "wv"},
    "5": {"la", "ms", "tx"},
    "6": {"ky", "mi", "oh", "tn"},
    "7": {"il", "in", "wi"},
    "8": {"ar", "ia", "mn", "mo", "ne", "nd", "sd"},
    "9": {"ak", "az", "ca", "hi", "id", "mt", "nv", "or", "wa", "gu"},
    "10": {"co", "ks", "nm", "ok", "ut", "wy"},
    "11": {"al", "fl", "ga"},
    "dc": {"dc"},
}

_STATE_ABBREV = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa", "west virginia": "wv",
    "wisconsin": "wi", "wyoming": "wy", "district of columbia": "dc",
}

_COURT_STATE_MAP = {
    "cal": "ca", "tex": "tx", "fla": "fl", "ill": "il", "ohio": "oh",
    "mich": "mi", "n.y": "ny", "n.j": "nj", "pa": "pa", "mass": "ma",
    "conn": "ct", "md": "md", "va": "va", "ga": "ga", "la": "la",
    "wash": "wa", "colo": "co", "ariz": "az", "ala": "al",
    "ind": "in", "minn": "mn", "mo": "mo", "wis": "wi", "iowa": "ia",
    "kan": "ks", "ky": "ky", "tenn": "tn", "miss": "ms", "ark": "ar",
    "neb": "ne", "nev": "nv", "n.m": "nm", "idaho": "id", "utah": "ut",
    "mont": "mt", "wyo": "wy", "s.d": "sd", "n.d": "nd", "me": "me",
    "n.h": "nh", "vt": "vt", "r.i": "ri", "del": "de", "haw": "hi",
    "alaska": "ak", "w.va": "wv", "s.c": "sc", "n.c": "nc", "okla": "ok",
}


def _detect_jurisdiction(text: str) -> dict:
    """Auto-detect the court jurisdiction from document headers."""
    header = text[:2000].upper()
    result = {"type": None, "circuit": None, "state": None, "court_name": None}

    # Federal circuit court
    circuit_match = re.search(
        r'(?:COURT\s+OF\s+APPEALS|CIRCUIT\s+COURT\s+OF\s+APPEALS)\s+'
        r'(?:FOR\s+THE\s+)?(\w+)\s+CIRCUIT',
        header,
    )
    if circuit_match:
        circuit_text = circuit_match.group(1).lower()
        circuit_map = {
            "first": "1", "second": "2", "third": "3", "fourth": "4",
            "fifth": "5", "sixth": "6", "seventh": "7", "eighth": "8",
            "ninth": "9", "tenth": "10", "eleventh": "11",
            "1st": "1", "2nd": "2", "2d": "2", "3rd": "3", "3d": "3",
            "4th": "4", "5th": "5", "6th": "6", "7th": "7", "8th": "8",
            "9th": "9", "10th": "10", "11th": "11",
        }
        circuit_num = circuit_map.get(circuit_text)
        if circuit_num:
            result["type"] = "federal_circuit"
            result["circuit"] = circuit_num
            result["court_name"] = f"{circuit_match.group(1).title()} Circuit"
            return result

    # Federal district court
    district_match = re.search(
        r'(?:UNITED\s+STATES\s+)?DISTRICT\s+COURT\s+'
        r'(?:FOR\s+THE\s+)?'
        r'(?:(?:NORTHERN|SOUTHERN|EASTERN|WESTERN|MIDDLE|CENTRAL)\s+)?'
        r'DISTRICT\s+OF\s+'
        r'([A-Z][A-Z\s]+)',
        header,
    )
    if district_match:
        state_name = district_match.group(1).strip().lower()
        state_abbrev = _STATE_ABBREV.get(state_name)
        if state_abbrev:
            for circ, states in _CIRCUIT_STATES.items():
                if state_abbrev in states:
                    result["type"] = "federal_district"
                    result["circuit"] = circ
                    result["state"] = state_abbrev
                    result["court_name"] = f"District of {state_name.title()}"
                    return result

    # State supreme court
    state_supreme = re.search(
        r'SUPREME\s+COURT\s+'
        r'(?:OF\s+(?:THE\s+)?(?:STATE\s+OF\s+)?)?'
        r'([A-Z][A-Z\s]+)',
        header,
    )
    if state_supreme:
        state_name = state_supreme.group(1).strip().lower()
        state_abbrev = _STATE_ABBREV.get(state_name)
        if state_abbrev:
            result["type"] = "state"
            result["state"] = state_abbrev
            result["court_name"] = f"Supreme Court of {state_name.title()}"
            return result

    # State appeals court
    state_appeals = re.search(
        r'(?:COURT\s+OF\s+APPEAL[S]?|APPELLATE\s+COURT)\s+'
        r'(?:OF\s+(?:THE\s+)?(?:STATE\s+OF\s+)?)?'
        r'([A-Z][A-Z\s]+)',
        header,
    )
    if state_appeals:
        state_name = state_appeals.group(1).strip().lower()
        state_abbrev = _STATE_ABBREV.get(state_name)
        if state_abbrev:
            result["type"] = "state"
            result["state"] = state_abbrev
            result["court_name"] = f"Court of Appeals of {state_name.title()}"
            return result

    # U.S. Supreme Court
    if re.search(r'SUPREME\s+COURT\s+OF\s+THE\s+UNITED\s+STATES', header):
        result["type"] = "scotus"
        result["court_name"] = "Supreme Court of the United States"
        return result

    return result


def _citation_is_in_jurisdiction(cite_court: str, jurisdiction: dict) -> bool:
    """Check if a citation's court is within the detected jurisdiction."""
    if not cite_court or not jurisdiction.get("type"):
        return True

    court_lower = cite_court.lower().strip()

    # SCOTUS citations are always in-jurisdiction
    if not court_lower:
        return True

    jur_type = jurisdiction["type"]
    if jur_type == "scotus":
        return True

    # Check for circuit match
    circuit_match = re.search(r'(\d+)(?:st|nd|rd|th)?\s*cir', court_lower)
    if circuit_match:
        cite_circuit = circuit_match.group(1)
        if jur_type in ("federal_circuit", "federal_district"):
            return cite_circuit == jurisdiction.get("circuit")
        if jur_type == "state":
            circuit_states = _CIRCUIT_STATES.get(cite_circuit, set())
            return jurisdiction.get("state") in circuit_states

    # D.C. Circuit
    if "d.c" in court_lower and "cir" in court_lower:
        if jur_type in ("federal_circuit", "federal_district"):
            return jurisdiction.get("circuit") == "dc"
        return False

    # Check for state court match
    for abbrev, state_code in _COURT_STATE_MAP.items():
        if abbrev in court_lower:
            if jur_type == "state":
                return state_code == jurisdiction.get("state")
            elif jur_type in ("federal_circuit", "federal_district"):
                circuit_states = _CIRCUIT_STATES.get(
                    jurisdiction.get("circuit", ""), set()
                )
                return state_code in circuit_states

    return True  # Can't determine, assume OK


def _citation_is_out_of_jurisdiction(
    cite_court: str,
    jurisdiction: dict,
    allow_other_state: bool = False,
    allow_federal: bool = False,
) -> bool:
    """Check if a citation's court is outside the acceptable jurisdiction.

    Rules (Florida default):
    - Florida state court → always OK
    - SCOTUS → always OK
    - Any federal court (including 11th Cir.) → OK only if allow_federal
    - Any other state court → OK only if allow_other_state
    """
    if not cite_court:
        return False  # Can't determine, assume OK

    court_lower = cite_court.lower().strip()
    if not court_lower:
        return False

    # SCOTUS is always in-jurisdiction
    # (SCOTUS citations typically have empty court field or just a year)

    # Check if it's a federal court citation
    is_federal = bool(
        re.search(r'\d+(?:st|nd|rd|th)?\s*cir', court_lower)
        or "d.c" in court_lower and "cir" in court_lower
        or re.search(r'\b[SNEWMC]\.?D\.?\s', cite_court)  # district courts
    )

    if is_federal:
        return not allow_federal  # out-of-jurisdiction unless federal allowed

    # Check if it's a Florida state court citation
    jur_state = jurisdiction.get("state", "fl")
    for abbrev, state_code in _COURT_STATE_MAP.items():
        if abbrev in court_lower:
            if state_code == jur_state:
                return False  # Same state = in-jurisdiction
            else:
                return not allow_other_state  # Other state = depends on flag

    # Check specifically for "Fla" in court field (common Florida abbreviation)
    if "fla" in court_lower:
        return False  # Florida citation

    return False  # Can't determine, assume OK


def _detect_out_of_jurisdiction(
    text: str,
    citations: list,
    allow_other_state: bool = False,
    allow_federal: bool = False,
) -> dict:
    """Criterion 6: Detect out-of-jurisdiction citations (max 8 pts).

    Defaults to Florida jurisdiction. SCOTUS is always in-jurisdiction.
    Federal courts (including 11th Cir.) require the allow_federal flag.
    Other state courts require the allow_other_state flag.
    """
    # If user says both other states and federal are OK, skip this criterion
    if allow_other_state and allow_federal:
        return {
            "points": 0,
            "max": 8,
            "detail": "All jurisdictions marked as acceptable by user",
            "jurisdiction": {"type": "state", "state": "fl",
                             "court_name": "Florida (assumed)"},
        }

    # Try auto-detecting jurisdiction from header; fall back to Florida
    jurisdiction = _detect_jurisdiction(text)
    if not jurisdiction.get("type"):
        jurisdiction = {
            "type": "state",
            "state": "fl",
            "circuit": "11",
            "court_name": "Florida (assumed)",
        }

    if not citations:
        return {
            "points": 0,
            "max": 8,
            "detail": (
                f"Jurisdiction: {jurisdiction.get('court_name', 'Unknown')}; "
                "no citations to check"
            ),
            "jurisdiction": jurisdiction,
        }

    out_count = 0
    out_examples = []
    for cite in citations:
        if _citation_is_out_of_jurisdiction(
            cite.court, jurisdiction, allow_other_state, allow_federal
        ):
            out_count += 1
            if len(out_examples) < 3:
                out_examples.append(f"{cite.parties} ({cite.court})")

    total = len(citations)
    ratio = out_count / total if total > 0 else 0

    if ratio < 0.2:
        pts = 0
    elif ratio < 0.4:
        pts = 3
    elif ratio < 0.6:
        pts = 5
    elif ratio < 0.8:
        pts = 6
    else:
        pts = 8

    detail = (
        f"Jurisdiction: {jurisdiction.get('court_name', 'Unknown')}; "
        f"{out_count}/{total} citations from other jurisdictions"
    )

    return {
        "points": pts,
        "max": 8,
        "detail": detail,
        "jurisdiction": jurisdiction,
    }


# --- Criterion 7: Sparse record citations (max 5 pts) ---

_RECORD_PATTERNS = [
    re.compile(r'\bR\.\s*(?:at\s+)?\d+'),
    re.compile(r'\(R\.\s*\d+\)'),
    re.compile(r'\bRecord\s+(?:at\s+)?\d+', re.IGNORECASE),
    re.compile(r'\bApp\.\s*(?:at\s+)?\d+'),
    re.compile(r'\bDkt\.\s*(?:No\.\s*)?\d+', re.IGNORECASE),
    re.compile(r'\bECF\s+(?:No\.\s*)?\d+', re.IGNORECASE),
    re.compile(r'\bDoc\.\s*(?:No\.\s*)?\d+', re.IGNORECASE),
    re.compile(r'\bTr\.\s*(?:at\s+)?\d+'),
    re.compile(r'\bJ\.?A\.\s*\d+'),
]


def _detect_sparse_record_citations(text: str) -> dict:
    """Criterion 7: Detect sparse record/trial citations (max 4 pts)."""
    word_count = len(text.split())
    if word_count < 500:
        return {
            "points": 0,
            "max": 4,
            "detail": "Document too short to evaluate record citations",
        }

    record_count = sum(len(p.findall(text)) for p in _RECORD_PATTERNS)
    expected = word_count / 300

    if record_count == 0 and word_count > 1000:
        pts = 4
        detail = "No record or docket citations found in a substantive brief"
    elif record_count < expected * 0.2:
        pts = 3
        detail = f"Very few record citations ({record_count} found, expected ~{int(expected)})"
    elif record_count < expected * 0.5:
        pts = 1
        detail = f"Somewhat sparse record citations ({record_count} found)"
    else:
        pts = 0
        detail = f"Adequate record citations ({record_count} found)"

    return {"points": pts, "max": 4, "detail": detail}


# --- Criterion 8: Repeating the same point (max 5 pts) ---

def _detect_repetition(text: str) -> dict:
    """Criterion 8: Detect repetitive arguments (max 4 pts)."""
    paragraphs = [p.strip() for p in text.split('\n') if len(p.strip()) > 80]
    if len(paragraphs) < 3:
        return {"points": 0, "max": 4, "detail": "Not enough paragraphs to analyze"}

    _stopwords = {
        "the", "and", "for", "that", "this", "with", "was", "are", "were",
        "has", "have", "had", "not", "but", "from", "they", "been", "will",
        "would", "could", "should", "may", "can", "its", "his", "her",
        "their", "our", "your", "any", "all", "each", "which", "when",
        "where", "how", "what", "who", "whom", "also", "than", "then",
        "more", "most", "such", "into", "over", "some", "other",
    }

    def word_set(para):
        words = set(re.findall(r'[a-z]{3,}', para.lower()))
        return words - _stopwords

    para_words = [word_set(p) for p in paragraphs]

    high_sim_pairs = 0
    for i in range(len(para_words)):
        for j in range(i + 1, len(para_words)):
            if not para_words[i] or not para_words[j]:
                continue
            intersection = para_words[i] & para_words[j]
            union = para_words[i] | para_words[j]
            jaccard = len(intersection) / len(union) if union else 0
            if jaccard > 0.5:
                high_sim_pairs += 1

    if high_sim_pairs == 0:
        pts = 0
        detail = "No repetitive sections detected"
    elif high_sim_pairs <= 2:
        pts = 2
        detail = f"Some repetition detected ({high_sim_pairs} similar paragraph pair(s))"
    else:
        pts = 4
        detail = f"Significant repetition ({high_sim_pairs} similar paragraph pairs)"

    return {"points": pts, "max": 4, "detail": detail}


# --- Criterion 9: Missing procedural posture (max 5 pts) ---

_PROCEDURAL_PATTERNS = [
    re.compile(r'\bmotion\s+(?:for|to)\s+\w+', re.IGNORECASE),
    re.compile(r'\bappeal\s+from\b', re.IGNORECASE),
    re.compile(r'\bthis\s+(?:case|action|matter)\s+arises\b', re.IGNORECASE),
    re.compile(
        r'\b(?:plaintiff|defendant|appellant|appellee|petitioner|respondent)'
        r'\s+(?:filed|moved|seeks|appeals)',
        re.IGNORECASE,
    ),
    re.compile(r'\bcourt\s+(?:granted|denied|dismissed|sustained|overruled)', re.IGNORECASE),
    re.compile(r'\bprocedural\s+(?:history|background|posture)\b', re.IGNORECASE),
    re.compile(r'\bstatement\s+of\s+(?:the\s+)?case\b', re.IGNORECASE),
    re.compile(r'\bfactual\s+(?:background|history)\b', re.IGNORECASE),
    re.compile(r'\bstandard\s+of\s+review\b', re.IGNORECASE),
    re.compile(r'\b(?:jury|bench)\s+trial\b', re.IGNORECASE),
    re.compile(r'\bsummary\s+judgment\b', re.IGNORECASE),
    re.compile(r'\b(?:remand|reverse|affirm|vacate)\b', re.IGNORECASE),
]


def _detect_missing_procedural_posture(text: str) -> dict:
    """Criterion 9: Detect missing procedural posture (max 4 pts)."""
    word_count = len(text.split())
    if word_count < 500:
        return {"points": 0, "max": 4, "detail": "Document too short to evaluate"}

    matches = sum(1 for p in _PROCEDURAL_PATTERNS if p.search(text))

    if matches >= 4:
        pts = 0
        detail = "Good procedural posture (multiple procedural references found)"
    elif matches >= 2:
        pts = 2
        detail = f"Minimal procedural context ({matches} procedural references)"
    elif matches >= 1:
        pts = 3
        detail = f"Very little procedural context ({matches} procedural reference)"
    else:
        pts = 4
        detail = "No procedural posture detected \u2014 brief lacks procedural history"

    return {"points": pts, "max": 4, "detail": detail}


# --- Criterion 10: Overly "helpful explainer" voice (max 5 pts) ---

_EXPLAINER_PHRASES = [
    r"it is important to note",
    r"this highlights the significance",
    r"it should be noted",
    r"this is particularly relevant",
    r"it bears mentioning",
    r"as previously mentioned",
    r"in today's legal landscape",
    r"this underscores (?:the|how)",
    r"delve into",
    r"shed(?:s|ding)? light on",
    r"navigat(?:e|ing) the (?:complex|intricate|nuanc)",
    r"in the realm of",
    r"it is worth (?:noting|mentioning|emphasizing)",
    r"this is especially (?:true|important|relevant|significant)",
    r"one cannot (?:overstate|underestimate|ignore)",
    r"plays a (?:crucial|vital|pivotal|significant) role",
    r"it is (?:crucial|essential|vital|imperative) (?:to|that)",
    r"serves as a (?:reminder|testament|cornerstone)",
    r"speaks volumes",
]


def _detect_explainer_voice(text: str) -> dict:
    """Criterion 10: Detect overly 'helpful explainer' voice (max 5 pts)."""
    text_lower = text.lower()
    count = 0
    found = []

    for phrase in _EXPLAINER_PHRASES:
        hits = re.findall(phrase, text_lower)
        count += len(hits)
        if hits and len(found) < 3:
            found.append(hits[0])

    if count == 0:
        pts = 0
        detail = "No 'explainer voice' phrases detected"
    elif count <= 2:
        pts = 2
        detail = f"Some explainer-style language ({count} instance(s))"
    elif count <= 4:
        pts = 3
        detail = f"Noticeable explainer voice ({count} instances)"
    else:
        pts = 5
        detail = (
            f"Strong explainer voice ({count} instances) \u2014 "
            "reads more like a blog post than legal advocacy"
        )

    return {"points": pts, "max": 5, "detail": detail}


# --- Criterion 11: Buzzwordy legal adjectives (max 5 pts) ---

_BUZZWORD_ADJECTIVES = [
    "well-settled", "well settled", "well-established", "well established",
    "firmly established", "deeply rooted", "longstanding", "long-standing",
    "time-honored", "fundamental", "robust", "abundantly clear",
    "crystal clear", "hornbook law", "black-letter",
]


def _detect_buzzword_adjectives(text: str) -> dict:
    """Criterion 11: Detect buzzwordy adjectives without authority (max 5 pts)."""
    text_lower = text.lower()
    unsupported = 0
    total = 0

    for bw in _BUZZWORD_ADJECTIVES:
        for m in re.finditer(re.escape(bw), text_lower):
            total += 1
            after = text[m.end():m.end() + 150]
            has_cite = bool(re.search(r'\d{1,4}\s+\w+\.', after))
            if not has_cite:
                unsupported += 1

    if unsupported == 0:
        pts = 0
        if total == 0:
            detail = "No buzzword adjectives found"
        else:
            detail = f"All {total} emphatic adjective(s) supported by citations"
    elif unsupported <= 2:
        pts = 2
        detail = f"{unsupported} buzzword adjective(s) without supporting authority"
    elif unsupported <= 4:
        pts = 3
        detail = f"{unsupported} buzzword adjectives without supporting authority"
    else:
        pts = 5
        detail = (
            f"{unsupported} buzzword adjectives used without citing authority \u2014 "
            "characteristic of AI-generated text"
        )

    return {"points": pts, "max": 5, "detail": detail}


# --- Criterion 12: Excessive em-dashes (max 5 pts) ---

def _detect_excessive_em_dashes(text: str) -> dict:
    """Criterion 12: Detect excessive em-dash usage (max 5 pts).

    AI-generated text (especially from ChatGPT) tends to overuse em-dashes.
    More than 4 em-dashes in a legal brief is unusual and suggestive of AI.
    """
    # Count both Unicode em-dash and double-hyphen stand-ins
    em_dash_count = text.count("\u2014")  # —
    # Also count en-dash used as em-dash (common in AI output)
    en_dash_count = text.count("\u2013")  # –
    # Double-hyphens used as em-dashes
    double_hyphen = len(re.findall(r'(?<!\-)\-\-(?!\-)', text))

    total = em_dash_count + en_dash_count + double_hyphen

    if total <= 4:
        pts = 0
        detail = f"{total} em-dash(es) found — within normal range"
    elif total <= 8:
        pts = 2
        detail = f"{total} em-dashes found — somewhat elevated for legal writing"
    elif total <= 15:
        pts = 3
        detail = f"{total} em-dashes found — unusual frequency for legal writing"
    else:
        pts = 5
        detail = (
            f"{total} em-dashes found — highly unusual for legal writing, "
            "characteristic of AI-generated text"
        )

    return {"points": pts, "max": 5, "detail": detail}


# --- Criterion 13: Excessive unnecessary hyphenation (max 5 pts) ---

# Pattern: adverb ending in -ly followed by a hyphen and an adjective/participle.
# Grammar rule: adverbs ending in -ly should NOT be hyphenated to the word they modify.
# AI models frequently violate this (e.g., "clearly-established" instead of "clearly established").
_UNNECESSARY_HYPHEN_LY = re.compile(
    r'\b(\w+ly)-(\w{3,})\b', re.IGNORECASE
)

# Words ending in -ly that are NOT adverbs (so hyphenation may be valid)
_FALSE_LY_ADVERBS = {
    "family", "only", "holy", "rally", "tally", "daily", "early",
    "friendly", "lonely", "lovely", "ugly", "likely", "elderly",
    "supply", "reply", "apply", "ally", "belly", "bully", "folly",
    "jelly", "jolly", "lily", "rally", "silly", "tally", "wily",
    "assembly", "homily", "anomaly", "monopoly", "italy",
}


def _detect_unnecessary_hyphens(text: str) -> dict:
    """Criterion 13: Detect excessive unnecessary hyphenation (max 5 pts).

    AI-generated text tends to over-hyphenate, joining words with hyphens
    where grammar does not require them. The most reliable signal is
    hyphenating adverbs ending in -ly to the adjective they modify
    (e.g., 'clearly-established' should be 'clearly established').
    """
    unnecessary_count = 0
    examples = []

    # Check for -ly adverb hyphenation errors
    for m in _UNNECESSARY_HYPHEN_LY.finditer(text):
        adverb = m.group(1).lower()
        if adverb not in _FALSE_LY_ADVERBS:
            unnecessary_count += 1
            if len(examples) < 3:
                examples.append(f'"{m.group(0)}"')

    if unnecessary_count == 0:
        pts = 0
        detail = "No unnecessary hyphenation detected"
    elif unnecessary_count <= 2:
        pts = 1
        detail = f"{unnecessary_count} unnecessarily hyphenated term(s): {'; '.join(examples)}"
    elif unnecessary_count <= 5:
        pts = 3
        detail = f"{unnecessary_count} unnecessarily hyphenated terms: {'; '.join(examples)}"
    else:
        pts = 5
        detail = (
            f"{unnecessary_count} unnecessarily hyphenated terms found — "
            f"e.g., {'; '.join(examples)}"
        )

    return {"points": pts, "max": 5, "detail": detail}


# --- Main scoring function ---

def compute_ai_score(
    text: str,
    citations: list = None,
    pro_se_override: bool = False,
    allow_other_state: bool = False,
    allow_federal: bool = False,
) -> dict:
    """
    Compute the AI-generation probability score on a 100-point scale.

    Args:
        text: Full document text.
        citations: List of Citation objects with verification results.
                   If None, citation-based criteria are scored as 0.
        pro_se_override: User indicated the drafter is pro se.
        allow_other_state: User indicated other state jurisdictions are acceptable.
        allow_federal: User indicated federal jurisdictions are acceptable.

    Returns a dict with total_score, auto_flagged, label, and criteria breakdown.
    """
    criteria = []

    # Criterion 1: Mismatched citations (max 10)
    if citations:
        mismatch_count = sum(1 for c in citations if c.status == "mismatch")
        if mismatch_count == 0:
            c1_pts, c1_detail = 0, "All citation names match database records"
        elif mismatch_count == 1:
            c1_pts, c1_detail = 5, "1 citation with mismatched case name"
        else:
            c1_pts, c1_detail = 10, f"{mismatch_count} citations with mismatched case names"
    else:
        c1_pts, c1_detail = 0, "Citations not yet verified"

    criteria.append({
        "name": "Mismatched Citation Names",
        "description": "Case names in the brief don\u2019t match the actual case names in the database",
        "points": c1_pts,
        "max": 10,
        "detail": c1_detail,
    })

    # Criterion 2: Non-existent citations (max 20) — AUTO FLAG
    auto_flagged = False
    if citations:
        not_found = sum(1 for c in citations if c.status == "not_found")
        if not_found == 0:
            c2_pts, c2_detail = 0, "All citations found in legal databases"
        elif not_found == 1:
            c2_pts = 10
            c2_detail = "1 citation not found \u2014 may be fabricated"
            auto_flagged = True
        else:
            c2_pts = 20
            c2_detail = f"{not_found} citations not found \u2014 likely fabricated"
            auto_flagged = True
    else:
        c2_pts, c2_detail = 0, "Citations not yet verified"

    criteria.append({
        "name": "Non-Existent Case Citations",
        "description": "Citations that cannot be found in any legal database \u2014 hallmark of AI fabrication",
        "points": c2_pts,
        "max": 20,
        "detail": c2_detail,
        "auto_flag": auto_flagged,
    })

    # Criterion 3: Improper formatting
    c3 = _detect_formatting_issues(text)
    criteria.append({
        "name": "Improper Citation Formatting",
        "description": "Citations with incorrect Bluebook formatting (missing periods, wrong spacing)",
        "points": c3["points"], "max": c3["max"], "detail": c3["detail"],
    })

    # Criterion 4: Pro se + legalese
    c4 = _detect_pro_se_legalese(text, pro_se_override=pro_se_override)
    criteria.append({
        "name": "Pro Se Litigant Using Complex Legalese",
        "description": "Self-represented litigant\u2019s brief uses unusually sophisticated legal language",
        "points": c4["points"], "max": c4["max"], "detail": c4["detail"],
    })

    # Criterion 5: Unusual syntax
    c5 = _detect_unusual_syntax(text)
    criteria.append({
        "name": "Unusual Syntax",
        "description": "Unnaturally uniform sentence lengths, excessive passive voice, or overly long sentences",
        "points": c5["points"], "max": c5["max"], "detail": c5["detail"],
    })

    # Criterion 6: Out-of-jurisdiction
    c6 = _detect_out_of_jurisdiction(
        text, citations or [],
        allow_other_state=allow_other_state,
        allow_federal=allow_federal,
    )
    criteria.append({
        "name": "Out-of-Jurisdiction Citations",
        "description": "Citations to cases from courts outside the brief\u2019s jurisdiction",
        "points": c6["points"], "max": c6["max"], "detail": c6["detail"],
    })

    # Criterion 7: Sparse record citations
    c7 = _detect_sparse_record_citations(text)
    criteria.append({
        "name": "Sparse Record Citations",
        "description": "Few or no references to the trial record, docket, or appendix",
        "points": c7["points"], "max": c7["max"], "detail": c7["detail"],
    })

    # Criterion 8: Repetition
    c8 = _detect_repetition(text)
    criteria.append({
        "name": "Repetitive Arguments",
        "description": "Multiple paragraphs making substantially the same point",
        "points": c8["points"], "max": c8["max"], "detail": c8["detail"],
    })

    # Criterion 9: Missing procedural posture
    c9 = _detect_missing_procedural_posture(text)
    criteria.append({
        "name": "Missing Procedural Posture",
        "description": "Brief lacks procedural history (motions, rulings, standard of review)",
        "points": c9["points"], "max": c9["max"], "detail": c9["detail"],
    })

    # Criterion 10: Explainer voice
    c10 = _detect_explainer_voice(text)
    criteria.append({
        "name": "Overly \u201cHelpful Explainer\u201d Voice",
        "description": "Uses blog-post-style phrases instead of legal advocacy language",
        "points": c10["points"], "max": c10["max"], "detail": c10["detail"],
    })

    # Criterion 11: Buzzword adjectives
    c11 = _detect_buzzword_adjectives(text)
    criteria.append({
        "name": "Buzzwordy Legal Adjectives",
        "description": "Overuse of \u201cwell-settled,\u201d \u201crobust,\u201d \u201cfundamental,\u201d etc. without citing authority",
        "points": c11["points"], "max": c11["max"], "detail": c11["detail"],
    })

    # Criterion 12: Excessive em-dashes
    c12 = _detect_excessive_em_dashes(text)
    criteria.append({
        "name": "Excessive Em-Dashes",
        "description": "Overuse of em-dashes (\u2014), which AI models use far more frequently than human legal writers",
        "points": c12["points"], "max": c12["max"], "detail": c12["detail"],
    })

    # Criterion 13: Unnecessary hyphenation
    c13 = _detect_unnecessary_hyphens(text)
    criteria.append({
        "name": "Excessive Unnecessary Hyphenation",
        "description": "Words joined with hyphens where grammar doesn\u2019t require them (e.g., \u201cclearly-established\u201d instead of \u201cclearly established\u201d)",
        "points": c13["points"], "max": c13["max"], "detail": c13["detail"],
    })

    # Sum and label
    total = min(sum(c["points"] for c in criteria), 100)

    if total == 0:
        label = "Not AI generated"
    elif total <= 10:
        label = "Low chance of AI generation"
    elif total <= 30:
        label = "Moderate chance of some AI generation"
    elif total <= 50:
        label = "High chance of some AI generation"
    elif total <= 80:
        label = "Moderate chance that entire brief was AI generated"
    else:
        label = "High chance that entire brief was AI generated"

    return {
        "total_score": total,
        "auto_flagged": auto_flagged,
        "label": label,
        "criteria": criteria,
    }


# ---------------------------------------------------------------------------
# Quotation extraction & verification
# ---------------------------------------------------------------------------

# Pattern for quoted text: straight quotes, curly quotes
_QUOTE_RE = re.compile(
    r'(?:'
    r'[\u201c](.+?)[\u201d]'   # curly quotes "..."
    r'|'
    r'"([^"]{40,}?)"'          # straight quotes "..."
    r')',
    re.DOTALL,
)

# Pattern for Id. / Ibid. references
_ID_RE = re.compile(r'\bId\.\s*(?:at\s+\d+)?', re.IGNORECASE)


def extract_quotes(text: str, citations: list) -> list:
    """Extract substantial quoted passages and attribute them to citations.

    Finds quoted text ≥ 40 characters and links each to the nearest citation
    (looking within ~300 chars after the quote, falling back to nearest before).
    Handles Id./Ibid. references by tracking the last-cited case.
    """
    if not citations:
        return []

    # Build a list of (position, citation_index) for all citations in the text
    cite_positions = []
    for i, cite in enumerate(citations):
        # Search for the volume+reporter+page pattern in the text
        pattern = re.escape(f"{cite.volume}") + r"\s+" + re.escape(cite.reporter) + r"\s+" + re.escape(f"{cite.page}")
        for m in re.finditer(pattern, text):
            cite_positions.append((m.start(), i))

    cite_positions.sort(key=lambda x: x[0])

    quotes = []
    last_cite_index = 0  # Track last-used citation for Id. references

    for m in _QUOTE_RE.finditer(text):
        quoted_text = m.group(1) or m.group(2)
        if not quoted_text or len(quoted_text.strip()) < 40:
            continue

        # Clean up the quoted text
        quoted_text = quoted_text.strip()
        quote_end = m.end()
        quote_start = m.start()

        # Look for a citation within ~300 chars after the closing quote
        after_text = text[quote_end:quote_end + 300]
        attributed_index = None

        # Check for Id. / Ibid. reference first
        id_match = _ID_RE.search(after_text[:80])
        if id_match:
            attributed_index = last_cite_index
        else:
            # Look for nearest citation after the quote
            for pos, idx in cite_positions:
                if pos >= quote_end and pos <= quote_end + 300:
                    attributed_index = idx
                    last_cite_index = idx
                    break

        # Fall back to nearest citation before the quote
        if attributed_index is None:
            for pos, idx in reversed(cite_positions):
                if pos <= quote_start:
                    attributed_index = idx
                    last_cite_index = idx
                    break

        if attributed_index is None:
            continue  # Can't attribute this quote

        cite = citations[attributed_index]
        cite_label = f"{cite.volume} {cite.reporter} {cite.page}"

        quotes.append(Quote(
            text=quoted_text[:500],  # Truncate very long quotes
            cite_index=attributed_index,
            cite_label=cite_label,
        ))

    return quotes


def _search_courtlistener_for_quote(
    search_phrase: str, session: requests.Session, court_filter: str = ""
) -> list | None:
    """Search CourtListener for an exact phrase, optionally filtered by court.

    Returns the list of result dicts, or None on error.
    """
    time.sleep(REQUEST_DELAY)
    try:
        params = {"q": f'"{search_phrase}"', "type": "o"}
        if court_filter:
            params["court"] = court_filter
        resp = session.get(SEARCH_URL, params=params, timeout=30)
    except requests.RequestException:
        return None

    if resp.status_code != 200:
        return None

    try:
        data = resp.json()
    except ValueError:
        return None

    return data.get("results", [])


def _check_results_for_cited_case(
    results: list, citation: Citation
) -> str | None:
    """Check if any search result matches the cited citation.

    Returns the case name if found, or None.
    """
    our_cite_norm = _normalize_reporter(citation.reporter)
    for result in results[:10]:
        case_name = result.get("caseName", "") or ""
        cite_strings = result.get("citation", [])
        for cite_str in cite_strings:
            m = CITE_STRING_RE.search(cite_str)
            if not m:
                continue
            vol, rptr, page = m.group(1), m.group(2), m.group(3)
            if (vol == citation.volume
                    and _normalize_reporter(rptr) == our_cite_norm
                    and page == citation.page):
                return case_name
    return None


def _extract_found_elsewhere(results: list) -> tuple[str, str]:
    """Extract case name and citation string from first search result.

    Returns (display_string, raw_cite_string) tuple.
    """
    first_result = results[0]
    found_name = first_result.get("caseName", "unknown case")
    found_cites = first_result.get("citation", [])
    found_cite_str = found_cites[0] if found_cites else ""

    display = (
        f'{found_name}, {found_cite_str}' if found_cite_str
        else found_name
    )
    return display, found_cite_str


def verify_quote(
    quote: Quote, citation: Citation, session: requests.Session
) -> Quote:
    """Verify a quoted passage against CourtListener and Google Scholar.

    Search order (jurisdiction-first):
      1. Search CourtListener filtered to the same jurisdiction as the cited
         reporter (e.g., So. 2d → Florida courts).
      2. If not found, search CourtListener with no jurisdiction filter.
      3. If still not found, try Google Scholar as a last resort.
    """
    # Use first ~12 words of the quote for the search
    words = quote.text.split()
    search_phrase = " ".join(words[:12])

    # Determine jurisdiction filter from the reporter
    court_filter = _get_jurisdiction_courts(citation.reporter)

    # --- Step 1: Search CourtListener (jurisdiction-filtered first) ---
    all_results = []

    if court_filter:
        # First: search within the same jurisdiction
        juris_results = _search_courtlistener_for_quote(
            search_phrase, session, court_filter
        )
        if juris_results:
            # Check if found in the cited case
            matched_name = _check_results_for_cited_case(juris_results, citation)
            if matched_name:
                quote.status = "verified"
                quote.detail = f'Quote verified in {matched_name}'
                return quote
            all_results = juris_results

    # Second: search CourtListener broadly (no jurisdiction filter)
    broad_results = _search_courtlistener_for_quote(search_phrase, session)
    if broad_results:
        matched_name = _check_results_for_cited_case(broad_results, citation)
        if matched_name:
            quote.status = "verified"
            quote.detail = f'Quote verified in {matched_name}'
            return quote
        # Prefer jurisdiction results if available, else use broad results
        if not all_results:
            all_results = broad_results

    if all_results:
        # Found in CourtListener but NOT in the cited case
        display, found_cite_str = _extract_found_elsewhere(all_results)
        quote.status = "found_elsewhere"
        quote.found_in = display
        quote.found_cite = found_cite_str  # Raw citation for similarity comparison
        quote.detail = f'Quote not found in cited case. Found in: {display}'
        return quote

    # --- Step 2: Try Google Scholar as fallback ---
    try:
        scholar_result = _search_google_scholar(search_phrase)
        if scholar_result:
            quote.status = "found_elsewhere"
            quote.found_in = scholar_result
            # Try to extract raw citation from the scholar result string
            scholar_cite_match = CITE_STRING_RE.search(scholar_result)
            quote.found_cite = scholar_cite_match.group(0) if scholar_cite_match else ""
            quote.detail = f'Not in CourtListener. Found via Google Scholar in: {scholar_result}'
            return quote
    except Exception:
        pass  # Scholar search is best-effort

    quote.status = "not_found"
    quote.detail = "Quote not found in any legal database — may be fabricated"
    return quote


def _search_google_scholar(phrase: str) -> str | None:
    """Search Google Scholar case law for an exact phrase.

    Returns a string with the case name and citation (if available), or None.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None

    url = "https://scholar.google.com/scholar"
    params = {
        "q": f'"{phrase}"',
        "hl": "en",
        "as_sdt": "2006",  # Case law only
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        # Google Scholar results: .gs_ri contains result info
        result_blocks = soup.select(".gs_ri")
        if not result_blocks:
            return None

        first = result_blocks[0]

        # Extract case name from the title element (.gs_rt)
        title_el = first.select_one(".gs_rt")
        if not title_el:
            return None
        case_name = title_el.get_text(strip=True)
        case_name = re.sub(r"^\[(?:PDF|HTML|BOOK)\]\s*", "", case_name)
        if not case_name:
            return None

        # Extract citation from the "green line" (.gs_a) — e.g.,
        # "Marbury v. Madison, 5 US 137 - Supreme Court, 1803"
        cite_str = ""
        green_line = first.select_one(".gs_a")
        if green_line:
            green_text = green_line.get_text(strip=True)
            # Try to find a citation pattern in the green line
            cite_match = CITE_STRING_RE.search(green_text)
            if cite_match:
                cite_str = cite_match.group(0)

        if cite_str:
            return f"{case_name}, {cite_str}"
        return case_name
    except Exception:
        return None

    return None


# ---------------------------------------------------------------------------
# Human error adjustment
# ---------------------------------------------------------------------------

def compute_human_error_adjustment(
    citations: list, quotes: list
) -> dict:
    """Analyze flagged items to distinguish human error from AI fabrication.

    Returns a dict with:
      - adjustment: net point change (negative = likely human error)
      - items: list of {description, classification, points} dicts
      - Each item is either 'human_error' or 'ai_indicator'

    For quotes found elsewhere, compares the found citation against the
    attributed citation.  Only classifies as human_error if citations are
    similar (same reporter, close volume/page — like a typo).  Otherwise
    it's an AI indicator.

    For case-name mismatches, checks if the correct citation for that case
    name is similar to the one given in the brief.
    """
    items = []

    # Check citations: not_found WITH suggestion = likely typo (human error)
    for cite in (citations or []):
        if cite.status == "not_found" and cite.suggestion:
            items.append({
                "description": (
                    f"Citation {cite.volume} {cite.reporter} {cite.page} not found — "
                    f"Did you mean {cite.suggestion}?"
                ),
                "classification": "human_error",
                "points": -10,
            })
        elif cite.status == "not_found" and not cite.suggestion:
            items.append({
                "description": (
                    f"Citation {cite.volume} {cite.reporter} {cite.page} not found — "
                    f"no similar case exists"
                ),
                "classification": "ai_indicator",
                "points": 0,  # Already scored by criterion #2
            })
        elif cite.status == "mismatch" and cite.suggestion:
            # Case name mismatch AND we found a suggestion — check similarity
            given_cite = f"{cite.volume} {cite.reporter} {cite.page}"
            if _citations_are_similar(given_cite, cite.suggestion):
                items.append({
                    "description": (
                        f"Case name mismatch for {given_cite} "
                        f"(found \"{cite.matched_case_name}\") — "
                        f"correct citation may be {cite.suggestion}; "
                        f"similar citation suggests human error"
                    ),
                    "classification": "human_error",
                    "points": -5,
                })
            else:
                items.append({
                    "description": (
                        f"Case name mismatch for {given_cite} "
                        f"(found \"{cite.matched_case_name}\") — "
                        f"correct citation is {cite.suggestion}; "
                        f"citations are too different for a simple typo"
                    ),
                    "classification": "ai_indicator",
                    "points": 3,
                })

    # Check quotes: found_elsewhere → compare citations for similarity
    for q in (quotes or []):
        if q.status == "found_elsewhere":
            # Build the attributed citation string for comparison
            attributed_cite = q.cite_label
            found_cite = q.found_cite  # Raw citation of where quote was found

            if found_cite and _citations_are_similar(attributed_cite, found_cite):
                # Same reporter, close volume/page — likely a typo
                items.append({
                    "description": (
                        f'Quote attributed to {q.cite_label} was actually found in: '
                        f'{q.found_in} — citations are similar (likely human error)'
                    ),
                    "classification": "human_error",
                    "points": -5,
                })
            else:
                # Different reporter or wildly different numbers — not a typo
                items.append({
                    "description": (
                        f'Quote attributed to {q.cite_label} was actually found in: '
                        f'{q.found_in} — citations are too different for a simple typo'
                    ),
                    "classification": "ai_indicator",
                    "points": 5,
                })
        elif q.status == "not_found":
            items.append({
                "description": (
                    f'Quote attributed to {q.cite_label} was not found '
                    f'in any legal database'
                ),
                "classification": "ai_indicator",
                "points": 5,
            })

    adjustment = sum(item["points"] for item in items)

    return {
        "adjustment": adjustment,
        "items": items,
    }


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
