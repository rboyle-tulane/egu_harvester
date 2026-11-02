# step 1: python -m pip install requests pandas beautifulsoup4 openpyxl
# step 2: python egusphere_harvester.py

import json
import re
import time
import hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import pandas as pd
from bs4 import BeautifulSoup


# ============================================================
# 1. CONFIGURATION
# ============================================================

PAPER_URL = (
    "https://egusphere.copernicus.org/preprints/2026/"
    "egusphere-2025-6519/"
)

PAPER_DOI = "10.5194/egusphere-2025-6519"

OUTPUT = Path("egusphere_harvest")
PDF_DIR = OUTPUT / "pdfs"
HTML_DIR = OUTPUT / "comment_pages"

for directory in [OUTPUT, PDF_DIR, HTML_DIR]:
    directory.mkdir(exist_ok=True, parents=True)

DELAY = 1.5
TIMEOUT = 45

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; academic-research/1.0)"
    )
}

session = requests.Session()
session.headers.update(HEADERS)

COMMENT_PATTERN = re.compile(
    r"egusphere-2025-6519-"
    r"(RC\d+|AC\d+|CC\d+|EC\d+|CEC\d+)",
    re.I,
)


# ============================================================
# 2. HTTP AND FILE UTILITIES
# ============================================================

def fetch(url):
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response


def safe_filename(value):
    value = re.sub(r"[^\w.-]+", "_", value)
    return value[:120] or "attachment"


def sha256_file(path):
    digest = hashlib.sha256()

    with open(path, "rb") as file:
        for chunk in iter(
            lambda: file.read(1024 * 1024), b""
        ):
            digest.update(chunk)

    return digest.hexdigest()


def is_pdf_response(response):
    content_type = response.headers.get(
        "Content-Type", ""
    ).lower()

    signature = response.content[:1024].lstrip()

    return (
        signature.startswith(b"%PDF-")
        or "application/pdf" in content_type
    )


# ============================================================
# 3. FIND DISCUSSION SECTION
# ============================================================

def get_discussion_section(soup):

    heading = soup.find(
        lambda tag: (
            tag.name in ["h1", "h2", "h3", "h4"]
            and "interactive discussion"
            in tag.get_text(" ", strip=True).lower()
        )
    )

    if not heading:
        raise RuntimeError(
            "Interactive discussion section not found."
        )

    node = heading

    for _ in range(6):
        if node.parent is None:
            break

        node = node.parent

        if COMMENT_PATTERN.search(str(node)):
            return node

    return heading.parent


# ============================================================
# 4. DISCOVER INDIVIDUAL COMMENTS
# ============================================================

def discover_comments(section):

    comments = {}

    for anchor in section.find_all("a", href=True):

        href = urljoin(PAPER_URL, anchor["href"])

        match = COMMENT_PATTERN.search(
            href + " " + anchor.get_text(" ", strip=True)
        )

        if not match:
            continue

        comment_id = match.group(1).upper()

        if comment_id in comments:
            continue

        container = anchor

        for _ in range(8):

            if container.parent is None:
                break

            container = container.parent

            if container.name in ["li", "article"]:
                break

            if (
                len(container.get_text(" ", strip=True)) > 100
                and COMMENT_PATTERN.search(str(container))
            ):
                break

        comments[comment_id] = {
            "comment_id": comment_id,
            "doi_url": (
                f"https://doi.org/"
                f"{PAPER_DOI}-{comment_id}"
            ),
            "page_url": href,
            "link_text": anchor.get_text(" ", strip=True),
            "inline_text": container.get_text("", strip=True),
            "page_text": "",
            "pdf_urls": [],
            "candidate_attachments": [],
            "downloaded_files": [],
            "pdf_available": False,
            "pdf_needed": False,
            "pdf_decision": "not_checked",
            "pdf_status": "not_checked",
        }

    return list(comments.values())


# ============================================================
# 5. FIND ATTACHMENT LINKS
# ============================================================

def extract_candidate_links(soup, page_url):

    candidates = []

    for anchor in soup.find_all("a", href=True):

        href = urljoin(page_url, anchor["href"])
        label = anchor.get_text(" ", strip=True)

        combined = (
            href + " "
            + label + " "
            + str(anchor.get("title", ""))
        ).lower()

        path = urlparse(href).path.lower()

        looks_like_attachment = (
            path.endswith(
                (".pdf", ".zip", ".doc", ".docx", ".xlsx", ".csv")
            )
            or "download" in combined
            or "attachment" in combined
            or "supplement" in combined
            or "pdf" in combined
        )

        if looks_like_attachment:
            candidates.append({
                "url": href,
                "label": label,
            })

    # Embedded PDF documents.

    for element in soup.find_all(
        ["iframe", "embed", "object"]
    ):

        src = element.get("src") or element.get("data")

        if src:
            candidates.append({
                "url": urljoin(page_url, src),
                "label": "Embedded attachment",
            })

    # Remove duplicate URLs.

    unique = {}

    for item in candidates:
        unique[item["url"]] = item

    return list(unique.values())


# ============================================================
# 6. VERIFY WHICH LINKS ARE ACTUAL PDFs
# ============================================================

def verify_pdf(url):

    try:

        response = session.get(
            url,
            headers={"Range": "bytes=0-1023"},
            timeout=TIMEOUT,
            stream=True,
            allow_redirects=True,
        )

        content_type = response.headers.get(
            "Content-Type", ""
        ).lower()

        signature = next(
            response.iter_content(1024), b""
        ).lstrip()

        response.close()

        return (
            signature.startswith(b"%PDF-")
            or "application/pdf" in content_type
        )

    except requests.RequestException as exc:

        print(f"Attachment verification failed: {exc}")
        return False


# ============================================================
# 7. DETERMINE WHETHER PDF DOWNLOAD IS NEEDED
# ============================================================

def determine_pdf_need(comment, confirmed_pdfs):

    text = (
        comment.get("inline_text", "") + " "
        + comment.get("page_text", "")
    ).lower()

    attachment_phrases = [
        "see comments in the attached pdf",
        "attached pdf",
        "attached file",
        "attached document",
        "see attached",
        "point-by-point responses",
        "comments are provided in the pdf",
        "comments in the attached",
        "please see the attached",
        "response in the attached",
    ]

    explicit_reference = any(
        phrase in text
        for phrase in attachment_phrases
    )

    pdf_available = bool(confirmed_pdfs)

    # Download when an actual PDF exists and either:
    # 1. The text explicitly references an attachment.
    # 2. The inline record is sparse.

    pdf_needed = (
        pdf_available
        and (
            explicit_reference
            or len(text.strip()) < 500
        )
    )

    if pdf_needed:
        decision = "download_pdf"

    elif pdf_available:
        decision = "optional_pdf"

    else:
        decision = "text_only"

    return {
        "pdf_available": pdf_available,
        "pdf_needed": pdf_needed,
        "explicit_attachment_reference": explicit_reference,
        "pdf_decision": decision,
    }


# ============================================================
# 8. INSPECT INDIVIDUAL COMMENT PAGES
# ============================================================

def inspect_comment(comment):

    print(f"Inspecting {comment['comment_id']}")

    try:

        response = fetch(comment["doi_url"])

        comment["resolved_url"] = response.url

        # Preserve original HTML.

        html_path = HTML_DIR / (
            comment["comment_id"] + ".html"
        )

        html_path.write_text(
            response.text,
            encoding="utf-8",
        )

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        comment["page_text"] = soup.get_text(
            "", strip=True
        )

        candidates = extract_candidate_links(
            soup,
            response.url,
        )

        comment["candidate_attachments"] = candidates

        confirmed_pdfs = []

        for candidate in candidates:

            if verify_pdf(candidate["url"]):
                confirmed_pdfs.append(candidate)

            time.sleep(0.5)

        comment["pdf_urls"] = confirmed_pdfs

        # Make PDF decision.

        decision = determine_pdf_need(
            comment,
            confirmed_pdfs,
        )

        comment.update(decision)

        comment["pdf_status"] = (
            "pdf_found"
            if comment["pdf_available"]
            else "no_pdf_found"
        )

    except requests.RequestException as exc:

        comment["pdf_status"] = "inspection_failed"
        comment["inspection_error"] = str(exc)

        print(f"Inspection failed: {exc}")

    return comment


# ============================================================
# 9. DOWNLOAD CONFIRMED PDF FILES
# ============================================================

def download_pdf(comment, attachment, index):

    url = attachment["url"]

    label = safe_filename(
        attachment.get("label", "report")
    )

    filename = (
        f"{comment['comment_id']}_{index}_{label}.pdf"
    )

    destination = PDF_DIR / filename

    if destination.exists():

        return {
            "file": str(destination),
            "sha256": sha256_file(destination),
            "status": "already_exists",
            "url": url,
        }

    try:

        response = fetch(url)

        if not is_pdf_response(response):

            return {
                "url": url,
                "status": "not_a_pdf",
            }

        destination.write_bytes(response.content)

        return {
            "file": str(destination),
            "sha256": sha256_file(destination),
            "status": "downloaded",
            "url": url,
            "bytes": destination.stat().st_size,
        }

    except requests.RequestException as exc:

        return {
            "url": url,
            "status": "download_failed",
            "error": str(exc),
        }


def process_downloads(comment):

    results = []

    for index, attachment in enumerate(
        comment.get("pdf_urls", []),
        start=1,
    ):

        print(
            f"Downloading {comment['comment_id']} "
            f"attachment {index}"
        )

        result = download_pdf(
            comment,
            attachment,
            index,
        )

        results.append(result)

        time.sleep(DELAY)

    comment["downloaded_files"] = results

    return comment


# ============================================================
# 10. EXPORT JSON, CSV AND EXCEL
# ============================================================

def export_results(comments):

    # Complete JSON archive.

    with open(
        OUTPUT / "discussion.json",
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            comments,
            file,
            indent=2,
            ensure_ascii=False,
        )

    # Main discussion table.

    rows = []

    for comment in comments:

        rows.append({
            "comment_id": comment["comment_id"],
            "doi_url": comment["doi_url"],
            "page_url": comment.get("resolved_url", ""),
            "link_text": comment["link_text"],
            "inline_text": comment["inline_text"],
            "page_text": comment.get("page_text", ""),
            "pdf_available": comment["pdf_available"],
            "pdf_needed": comment["pdf_needed"],
            "pdf_decision": comment["pdf_decision"],
            "pdf_status": comment["pdf_status"],
            "pdf_urls": json.dumps(
                comment["pdf_urls"]
            ),
            "downloaded_files": json.dumps(
                comment["downloaded_files"]
            ),
        })

    df = pd.DataFrame(rows)

    df.to_csv(
        OUTPUT / "discussion.csv",
        index=False,
        encoding="utf-8-sig",
    )

    # Separate attachment inventory.

    pdf_rows = []

    for comment in comments:

        for attachment in comment["pdf_urls"]:

            pdf_rows.append({
                "comment_id": comment["comment_id"],
                "url": attachment["url"],
                "label": attachment["label"],
                "download_decision": comment["pdf_decision"],
            })

    pdf_df = pd.DataFrame(pdf_rows)

    # Excel workbook.

    with pd.ExcelWriter(
        OUTPUT / "discussion.xlsx",
        engine="openpyxl",
    ) as writer:

        df.to_excel(
            writer,
            sheet_name="Discussion",
            index=False,
        )

        pdf_df.to_excel(
            writer,
            sheet_name="PDF inventory",
            index=False,
        )


# ============================================================
# 11. MAIN
# ============================================================

def main():

    print("=" * 60)
    print("EGUSPHERE DISCUSSION HARVESTER")
    print("=" * 60)

    print("Fetching main paper page...")

    response = fetch(PAPER_URL)

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    section = get_discussion_section(soup)

    comments = discover_comments(section)

    print(
        f"Discovered {len(comments)} discussion records."
    )

    if not comments:

        raise RuntimeError(
            "No comments discovered. "
            "Check the saved page and discussion selectors."
        )

    processed = []

    for comment in comments:

        comment = inspect_comment(comment)

        # Only download if the decision says required.

        if comment.get("pdf_needed", False):
            comment = process_downloads(comment)

        processed.append(comment)

        time.sleep(DELAY)

    export_results(processed)

    # Summary.

    print("=" * 60)
    print("HARVEST SUMMARY")
    print("=" * 60)

    for comment in processed:

        print(
            f"{comment['comment_id']}: "
            f"PDF available={comment['pdf_available']} | "
            f"PDF needed={comment['pdf_needed']} | "
            f"Decision={comment['pdf_decision']}"
        )

    print("Output directory:")
    print(OUTPUT.resolve())

    print("Completed.")


if __name__ == "__main__":
    main()
