#!/usr/bin/env python3
"""
url_to_markdown.py

Converts a URL (or raw HTML string) to clean Markdown.

Architecture (maps directly to the original JS modules):
  url_to_markdown_readers.js      → fetch_url(), process_stackoverflow(),
                                    fetch_apple_dev_doc()
  url_to_markdown_processor.js    → process_html()
  url_to_markdown_common_filters.js → strip_scripts_and_styles(),
                                      apply_domain_filters()
  url_to_markdown_formatters.js   → format_codeblocks(), format_tables()
  html_table_to_markdown.js       → convert_table()
  url_to_markdown_apple_dev_docs.js → apple_dev_doc_url(),
                                      parse_apple_dev_doc_json()
  index.js                        → url_to_markdown() (the main entry point)

Pipeline:
  URL → fetch_url()
      → strip_scripts_and_styles()
      → extract_main_content()   (Readability; falls back to full HTML)
      → format_codeblocks()      (replace <pre> with placeholders)
      → format_tables()          (replace <table> with placeholders)
      → html_to_markdown()       (markdownify + placeholder substitution)
      → apply_domain_filters()   (global + site-specific regex cleanup)
      → (optional) prepend title
"""

import html as html_module
import random
import re
import sys
from urllib.parse import urlparse

import markdownify
import requests
from bs4 import BeautifulSoup

# readability-lxml is optional but strongly preferred
try:
    from readability import Document as ReadabilityDocument
    HAS_READABILITY = True
except ImportError:
    HAS_READABILITY = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TIMEOUT_SECONDS = 15
USER_AGENT = "Urltomarkdown/1.0"

# Tables wider than this (total column chars) are rendered as indented lists
MAX_TABLE_WIDTH = 96

# If Readability returns fewer than this many characters we fall back to the
# full, cleaned HTML so we don't lose content.
MIN_CONTENT_LENGTH = 200

APPLE_DEV_PREFIX = "https://developer.apple.com"
STACKOVERFLOW_PREFIX = "https://stackoverflow.com/questions"


# ===========================================================================
# 1. Fetching
# ===========================================================================

def fetch_url(url: str) -> str:
    """Fetch raw HTML from *url*.

    Raises:
        requests.HTTPError      – non-2xx HTTP status
        requests.Timeout        – request timed out
        requests.RequestException – other network error
    """
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT_SECONDS,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.text


# ===========================================================================
# 2. Stripping scripts and styles
# ===========================================================================

def strip_scripts_and_styles(html: str) -> str:
    """Remove all <script>…</script> and <style>…</style> blocks.

    This is done with a regex pass before any DOM parsing so that JS/CSS
    content cannot accidentally end up in the final Markdown.
    """
    html = re.sub(r'<style[\s\S]*?</style>', '', html, flags=re.IGNORECASE)
    html = re.sub(r'<script[\s\S]*?</script>', '', html, flags=re.IGNORECASE)
    return html


# ===========================================================================
# 3. Extracting main content
# ===========================================================================

# HTML elements that are rarely part of the article body
_NOISE_TAGS = {"nav", "footer", "aside", "button", "header", "script", "style"}


def clean_html(soup: BeautifulSoup) -> BeautifulSoup:
    """Remove noise elements (nav, footer, aside, buttons, headers, scripts,
    styles) from *soup* in-place.  Returns the modified soup object."""
    for tag in soup.find_all(_NOISE_TAGS):
        tag.decompose()
    return soup


def extract_main_content(html: str, url: str = "", use_readability: bool = True) -> tuple:
    """Extract the main article body from *html*.

    Returns:
        (content_html: str, title: str)

    Strategy:
    1. If *use_readability* is True and readability-lxml is installed, run
       Mozilla Readability on the page.
    2. Quality checks: if the extracted text is shorter than
       MIN_CONTENT_LENGTH characters, fall through to the fallback.
    3. Fallback: parse the full HTML with BeautifulSoup, strip noise elements,
       and return the body innerHTML.
    """
    # Always extract the <title> from the raw HTML for use as the page title
    soup = BeautifulSoup(html, "lxml")
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    if use_readability and HAS_READABILITY:
        try:
            doc = ReadabilityDocument(html, url=url)
            content = doc.summary(html_partial=False)

            # Quality check: if Readability found substantial content, use it
            content_text = BeautifulSoup(content, "lxml").get_text(strip=True)
            if len(content_text) >= MIN_CONTENT_LENGTH:
                return content, title
            # Otherwise fall through to the fallback below
        except Exception:
            pass  # Readability failed; fall through to fallback

    # Fallback: return the cleaned full HTML
    clean_html(soup)
    body = soup.find("body")
    fallback_html = str(body) if body else str(soup)
    return fallback_html, title


# ===========================================================================
# 4. Table conversion  (html_table_to_markdown.js)
# ===========================================================================

def _clean_cell(cell_html: str) -> str:
    """Strip HTML tags from a table cell, collapse newlines, decode entities."""
    text = re.sub(r'</?[^>]+(>|$)', '', cell_html)
    text = re.sub(r'[\r\n]+', ' ', text)
    text = html_module.unescape(text)
    return text.strip()


def convert_table(table_html: str) -> str:
    """Convert an HTML table string to a Markdown table.

    If the total column width exceeds MAX_TABLE_WIDTH the table is rendered as
    an indented bullet list instead (mirrors html_table_to_markdown.js).

    Returns an empty string for degenerate tables (< 2 rows).
    """
    result = "\n"

    # Optional table caption
    caption_match = re.search(
        r'<caption[^>]*>([\s\S]*?)</caption>', table_html, re.IGNORECASE
    )
    if caption_match:
        result += _clean_cell(caption_match.group(1)) + "\n\n"

    # Collect rows
    rows_raw = re.findall(r'<tr[^>]*>[\s\S]*?</tr>', table_html, re.IGNORECASE)
    n_rows = len(rows_raw)
    if n_rows < 2:
        # Not a proper data table; skip it
        return ""

    items = []
    for row_html in rows_raw:
        cells = re.findall(r'<t[hd][^>]*>([\s\S]*?)</t[hd]>', row_html, re.IGNORECASE)
        items.append([_clean_cell(c) for c in cells])

    # Find the maximum column count across all rows
    n_cols = max((len(row) for row in items), default=0)
    if n_cols == 0:
        return ""

    # Normalise: pad short rows with empty strings
    for row in items:
        while len(row) < n_cols:
            row.append("")

    # Compute per-column widths (minimum 3 to fit the separator "---")
    col_widths = [3] * n_cols
    for row in items:
        for c, cell in enumerate(row):
            if len(cell) > col_widths[c]:
                col_widths[c] = len(cell)

    total_width = sum(col_widths)

    if total_width < MAX_TABLE_WIDTH:
        # ── Markdown pipe table ──────────────────────────────────────────────
        # Pad each cell to its column width
        padded = [
            [cell.ljust(col_widths[c]) for c, cell in enumerate(row)]
            for row in items
        ]
        # Header row
        result += "|" + "|".join(padded[0]) + "|\n"
        # Separator row
        result += "|" + "|".join("-" * w for w in col_widths) + "|\n"
        # Data rows
        for row in padded[1:]:
            result += "|" + "|".join(row) + "|\n"
    else:
        # ── Indented bullet list (fallback for wide tables) ─────────────────
        header = items[0]
        result += "\n"
        for row in items[1:]:
            if header[0] or row[0]:
                result += "* "
            if header[0]:
                result += header[0] + ": "
            if row[0]:
                result += row[0]
            if header[0] or row[0]:
                result += "\n"
            for c in range(1, n_cols):
                if header[c] or row[c]:
                    result += "  * "
                if header[c]:
                    result += header[c] + ": "
                if row[c]:
                    result += row[c]
                if header[c] or row[c]:
                    result += "\n"

    return result


# ===========================================================================
# 5. Pre-processing: code blocks and tables  (url_to_markdown_formatters.js)
# ===========================================================================

def format_codeblocks(html: str, replacements: list) -> str:
    """Replace every <pre>…</pre> block with a unique placeholder string.

    The real Markdown fenced code block is stored in *replacements* and is
    substituted back after the HTML→Markdown conversion so that markdownify
    cannot mangle the code content.

    Language detection: if the <pre> or inner <code> tag has a class like
    ``language-python`` or ``lang-js`` we carry that language identifier
    into the fence (e.g. ```python … ```).
    """
    def _convert(match: re.Match) -> str:
        block = match.group(0)

        # Try to detect a programming language from the CSS class
        lang = ""
        lang_match = re.search(
            r'<(?:pre|code)[^>]+class="[^"]*(?:language|lang)-([a-zA-Z0-9+#.-]+)',
            block,
            re.IGNORECASE,
        )
        if lang_match:
            lang = lang_match.group(1)

        # Normalise in-block line breaks before stripping tags
        block = re.sub(r'<br[^>]*>', '\n', block, flags=re.IGNORECASE)
        block = re.sub(r'<p>', '\n', block, flags=re.IGNORECASE)

        # Strip all remaining HTML tags
        text = re.sub(r'</?[^>]+(>|$)', '', block)

        # Decode HTML entities (e.g. &amp; → &)
        text = html_module.unescape(text)

        markdown = f"```{lang}\n{text}\n```\n"
        placeholder = f"urltomarkdowncodeblockplaceholder{len(replacements)}{random.random()}"
        replacements.append({"placeholder": placeholder, "replacement": markdown})
        return f"<p>{placeholder}</p>"

    return re.sub(r'<pre[^>]*>[\s\S]*?</pre>', _convert, html, flags=re.IGNORECASE)


def format_tables(html: str, replacements: list) -> str:
    """Replace every <table>…</table> block with a unique placeholder string.

    The converted Markdown table (or indented list) is stored in *replacements*
    and substituted back after the HTML→Markdown conversion.
    """
    def _convert(match: re.Match) -> str:
        table_html = match.group(0)
        markdown = convert_table(table_html)
        placeholder = f"urltomarkdowntableplaceholder{len(replacements)}{random.random()}"
        replacements.append({"placeholder": placeholder, "replacement": markdown})
        return f"<p>{placeholder}</p>"

    return re.sub(r'<table[^>]*>[\s\S]*?</table>', _convert, html, flags=re.IGNORECASE)


# ===========================================================================
# 6. HTML → Markdown
# ===========================================================================

def html_to_markdown(html: str, replacements: list = None) -> str:
    """Convert *html* to Markdown using markdownify, then substitute any
    placeholder strings that were created by format_codeblocks / format_tables.
    """
    md = markdownify.markdownify(
        html,
        heading_style=markdownify.ATX,  # use # / ## / ### style headings
        bullets="-",                    # bullet character for unordered lists
        strip=["script", "style"],      # drop any remaining script/style nodes
    )

    # Restore pre-computed code blocks and tables
    if replacements:
        for item in replacements:
            md = md.replace(item["placeholder"], item["replacement"])

    return md


# ===========================================================================
# 7. Domain-specific filters  (url_to_markdown_common_filters.js)
# ===========================================================================

# Each entry in this list is applied when the URL hostname matches *domain*.
# The global entry (domain=.*) is always applied first.
_DOMAIN_FILTERS = [
    {
        # ── Global filters applied to every page ───────────────────────────
        "domain": re.compile(r'.*'),
        "remove": [
            # Section-anchor paragraph marks like [¶](#heading "Permalink")
            re.compile(r'\[¶\]\(#[^\s]+ "[^"]+"\)'),
        ],
        "replace": [
            {
                # Unwanted whitespace inside link text: [ text ](url) → [text](url)
                "find": re.compile(r'\[[\n\s]*([^\]\n]*)[\n\s]*\]\(([^\)]*)\)'),
                "replacement": r'[\1](\2)',
            },
            {
                # Links stuck together: )[  →  )\n[
                "find": re.compile(r'\)\['),
                "replacement": ')\n[',
            },
            {
                # Missing URI scheme: [text](//host/path) → [text](https://host/path)
                "find": re.compile(r'\[([^\]]*)\]\(\/\/([^\)]*)\)'),
                "replacement": r'[\1](https://\2)',
            },
        ],
    },
    {
        # ── Wikipedia ──────────────────────────────────────────────────────
        "domain": re.compile(r'.*\.wikipedia\.org'),
        "remove": [
            re.compile(r'\*\*\[\^\]\(#cite_ref[^\)]+\)\*\*'),
            re.compile(r'(?:\\\[)?\[edit\]\([^\s]+ "[^"]+"\)(?:\\\])?', re.IGNORECASE),
            re.compile(r'\^\s\[Jump up to[^\)]*\)', re.IGNORECASE),
            re.compile(r'\[[^\]]*\]\(#cite_ref[^\)]+\)'),
            re.compile(r'\[\!\[Edit this at Wikidata\].*'),
            re.compile(
                r'\[\!\[Listen to this article\]\([^\)]*\)\]\([^\)]*\.(mp3|ogg|oga|flac)[^\)]*\)',
                re.IGNORECASE,
            ),
            re.compile(r'\[This audio file\]\([^\)]*\).*'),
            re.compile(r'\!\[Spoken Wikipedia icon\]\([^\)]*\)'),
            re.compile(r'\[.*\]\(.*Play audio.*\).*'),
        ],
        "replace": [
            {
                # Use the full-size image instead of the thumbnail
                "find": re.compile(
                    r'\(https://upload\.wikimedia\.org/wikipedia/([^/]+)/thumb/([^\)]+\..{3,4})/[^\)]+\)',
                    re.IGNORECASE,
                ),
                "replacement": r'(https://upload.wikimedia.org/wikipedia/\1/\2)',
            },
            {
                # Fix over-long setext underlines generated from Wikipedia tables
                "find": re.compile(r'\n(.+)\n-{32,}\n', re.IGNORECASE),
                "replacement": lambda m: (
                    '\n' + m.group(1) + '\n' + '-' * len(m.group(1)) + '\n'
                ),
            },
        ],
    },
    {
        # ── Medium ─────────────────────────────────────────────────────────
        "domain": re.compile(r'(?:.*\.)?medium\.com'),
        "replace": [
            {
                # Fix truncated Medium CDN image URLs
                "find": "(https://miro.medium.com/max/60/",
                "replacement": "(https://miro.medium.com/max/600/",
            },
            {
                # Unwrap nested image+link into a clean image + caption link
                "find": re.compile(
                    r'\s*\[\s*!\[([^\]]+)\]\(([^\)]+)\)\s*\]\(([^\?\)]*)\?[^\)]*\)\s*'
                ),
                "replacement": r'\n![\1](\2)\n[\1](\3)\n\n',
            },
        ],
    },
    {
        # ── Stack Overflow ─────────────────────────────────────────────────
        "domain": re.compile(r'(?:.*\.)?stackoverflow\.com'),
        "remove": [
            re.compile(r'\* +Links(.|\r|\n)*Three +\|'),
        ],
    },
]


def apply_domain_filters(url: str, markdown: str, ignore_links: bool = False) -> str:
    """Apply global and domain-specific regex filters to *markdown*.

    Also:
    - Converts relative URLs to absolute ones using the page's base address.
    - Strips inline links if *ignore_links* is True.
    """
    parsed = urlparse(url) if url else None
    domain = (parsed.hostname or "") if parsed else ""
    base_address = f"{parsed.scheme}://{parsed.hostname}" if parsed else ""

    for entry in _DOMAIN_FILTERS:
        if entry["domain"].search(domain):
            # Remove patterns
            for pattern in entry.get("remove", []):
                markdown = re.sub(pattern, "", markdown)

            # Replace patterns
            for rep in entry.get("replace", []):
                find = rep["find"]
                replacement = rep["replacement"]
                if isinstance(find, str):
                    # Plain string replacement
                    markdown = markdown.replace(find, replacement)
                elif callable(replacement):
                    # Regex with a callable replacement function
                    markdown = re.sub(find, replacement, markdown)
                else:
                    # Regex with a string replacement (may contain back-references)
                    markdown = re.sub(find, replacement, markdown)

    # Make relative URLs absolute: [text](/path) → [text](https://host/path)
    if base_address:
        def _make_absolute(m: re.Match) -> str:
            return f"[{m.group(1)}]({base_address}/{m.group(2)})"

        markdown = re.sub(
            r'\[([^\]]*)\]\(\/([^\/][^\)]*)\)',
            _make_absolute,
            markdown,
        )

    # Strip link markup when the caller wants plain text output
    if ignore_links:
        markdown = re.sub(r'\[\[?([^\]]+\]?)\]\([^\)]+\)', r'\1', markdown)
        markdown = re.sub(r'[\\\[]+([0-9]+)[\\\]]+', r'[\1]', markdown)

    return markdown


# ===========================================================================
# 8. Apple Developer Documentation  (url_to_markdown_apple_dev_docs.js)
# ===========================================================================

def apple_dev_doc_url(url: str) -> str:
    """Map an Apple Developer *url* to its JSON API endpoint.

    Example:
      https://developer.apple.com/documentation/swift/array
      → https://developer.apple.com/tutorials/data/documentation/swift/array.json
    """
    queryless = url.split('?')[0].rstrip('/')
    parts = queryless.split('/')
    json_url = "https://developer.apple.com/tutorials/data"
    for part in parts[3:]:
        json_url += "/" + part
    json_url += ".json"
    return json_url


def _process_content_section(section: dict, dev_references: dict, ignore_links: bool) -> str:
    """Recursively convert an Apple Dev Doc content section to Markdown."""
    text = ""
    for content in section.get("content", []):
        content_type = content.get("type")

        if content_type == "paragraph":
            inline_text = ""
            for inline in content.get("inlineContent", []):
                inline_type = inline.get("type")
                if inline_type == "text":
                    inline_text += inline.get("text", "")
                elif inline_type == "link":
                    if ignore_links:
                        inline_text += inline.get("title", "")
                    else:
                        inline_text += (
                            f"[{inline.get('title', '')}]"
                            f"({inline.get('destination', '')})"
                        )
                elif inline_type == "reference":
                    identifier = inline.get("identifier", "")
                    ref = dev_references.get(identifier, {})
                    inline_text += ref.get("title", "")
                elif inline_type == "codeVoice":
                    inline_text += f"`{inline.get('code', '')}`"
            text += inline_text + "\n\n"

        elif content_type == "codeListing":
            code_text = "\n```\n"
            code_text += "\n".join(content.get("code", []))
            code_text += "\n```\n\n"
            text += code_text

        elif content_type == "unorderedList":
            for list_item in content.get("items", []):
                text += "* " + _process_content_section(list_item, dev_references, ignore_links)

        elif content_type == "orderedList":
            for n, list_item in enumerate(content.get("items", []), start=1):
                text += f"{n}. " + _process_content_section(
                    list_item, dev_references, ignore_links
                )

        elif content_type == "heading":
            level = content.get("level", 2)
            heading_text = content.get("text", "")
            text += "#" * level + " " + heading_text + "\n\n"

    return text


def _process_sections(sections: list, dev_references: dict, ignore_links: bool) -> str:
    """Convert a list of Apple Dev Doc sections to Markdown."""
    text = ""
    for section in sections:
        kind = section.get("kind")

        if kind == "declarations":
            for declaration in section.get("declarations", []):
                tokens = declaration.get("tokens", [])
                if tokens:
                    text += "".join(t.get("text", "") for t in tokens)
                languages = declaration.get("languages", [])
                if languages:
                    text += " \nLanguages: " + ", ".join(languages)
                platforms = declaration.get("platforms", [])
                if platforms:
                    text += " \nPlatforms: " + ", ".join(platforms)
            text += "\n\n"

        elif kind == "content":
            text += _process_content_section(section, dev_references, ignore_links)

        section_title = section.get("title")
        if section_title:
            if kind == "hero":
                text += "# " + section_title + "\n"
            else:
                text += "## " + section_title

        for section_content in section.get("content", []):
            if section_content.get("type") == "text":
                text += section_content.get("text", "") + "\n"

    return text


def parse_apple_dev_doc_json(json_data: dict, options: dict) -> str:
    """Convert a parsed Apple Developer Documentation JSON object to Markdown."""
    inline_title = options.get("inline_title", True)
    ignore_links = options.get("ignore_links", False)
    text = ""

    if inline_title:
        title = json_data.get("metadata", {}).get("title", "")
        if title:
            text += "# " + title + "\n\n"

    dev_references = json_data.get("references", {})

    if "primaryContentSections" in json_data:
        text += _process_sections(
            json_data["primaryContentSections"], dev_references, ignore_links
        )
    elif "sections" in json_data:
        text += _process_sections(json_data["sections"], dev_references, ignore_links)

    return text


def fetch_apple_dev_doc(url: str, options: dict) -> str:
    """Fetch and convert an Apple Developer Documentation page to Markdown."""
    json_url = apple_dev_doc_url(url)
    response = requests.get(
        json_url,
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return parse_apple_dev_doc_json(response.json(), options)


# ===========================================================================
# 9. Stack Overflow special handling  (url_to_markdown_readers.js)
# ===========================================================================

def process_stackoverflow(url: str, options: dict) -> str:
    """Fetch a Stack Overflow question page and return question + best answer.

    The JS reader splits the page by DOM id ("question" and "answers") and
    processes each independently so Readability doesn't merge them.
    """
    html = fetch_url(url)
    html = strip_scripts_and_styles(html)
    soup = BeautifulSoup(html, "lxml")

    # Extract question and answer blocks by their well-known DOM ids
    question_el = soup.find(id="question")
    answers_el = soup.find(id="answers")

    question_html = str(question_el) if question_el else html
    answers_html = str(answers_el) if answers_el else ""

    # Process question (Readability disabled – content is already scoped)
    q_options = {**options, "use_readability": False}
    markdown_q = process_html(question_html, url=url, options=q_options)

    # Process answers
    if answers_html:
        a_options = {**options, "inline_title": False, "use_readability": False}
        markdown_a = process_html(answers_html, url=url, options=a_options)
    else:
        markdown_a = ""

    # If there are no real answers yet, return only the question
    if not markdown_a or markdown_a.startswith("Your Answer"):
        return markdown_q

    return markdown_q + "\n\n## Answer\n" + markdown_a


# ===========================================================================
# 10. Core processing pipeline  (url_to_markdown_processor.js)
# ===========================================================================

def process_html(html: str, url: str = "", options: dict = None) -> str:
    """Convert an HTML string to Markdown.

    This is the shared processing pipeline used by both the URL-fetching path
    and any direct-HTML-input path.

    Steps:
    1. Strip <script> and <style> blocks.
    2. Extract main content with Readability (fallback: cleaned full HTML).
    3. Pre-process <pre> blocks → fenced code block placeholders.
    4. Pre-process <table> blocks → Markdown table placeholders.
    5. Convert remaining HTML to Markdown with markdownify.
    6. Substitute placeholders.
    7. Apply global + domain-specific regex filters.
    8. Optionally prepend the page <title>.
    """
    if options is None:
        options = {}

    inline_title = options.get("inline_title", True)
    ignore_links = options.get("ignore_links", False)
    use_readability = options.get("use_readability", True)

    # Step 1 – strip scripts and styles
    html = strip_scripts_and_styles(html)

    # Step 2 – extract main content (Readability → fallback to full HTML)
    content, title = extract_main_content(html, url=url, use_readability=use_readability)

    # Steps 3 & 4 – pre-process code blocks and tables into placeholders
    replacements: list = []
    content = format_codeblocks(content, replacements)
    content = format_tables(content, replacements)

    # Step 5 & 6 – convert to Markdown and restore placeholders
    markdown = html_to_markdown(content, replacements)

    # Step 7 – domain-specific regex cleanup
    markdown = apply_domain_filters(url, markdown, ignore_links=ignore_links)

    # Step 8 – prepend the page title as an H1 if requested
    if inline_title and title:
        markdown = f"# {title}\n{markdown}"

    return markdown


# ===========================================================================
# 11. Main entry point  (url_to_markdown_readers.js + index.js)
# ===========================================================================

def url_to_markdown(
    url: str,
    inline_title: bool = True,
    ignore_links: bool = False,
    use_readability: bool = True,
) -> str:
    """Convert a URL to Markdown.

    Dispatches to the appropriate reader based on the URL:
    - Apple Developer docs  → JSON API reader (no HTML involved)
    - Stack Overflow pages  → split question / answer reader
    - Everything else       → standard HTML reader

    Args:
        url:             The URL to convert.
        inline_title:    When True, prepend the page <title> as an H1.
        ignore_links:    When True, strip all hyperlink markup from the output.
        use_readability: When True, use Mozilla Readability to extract the
                         main article body.  Disable to get the full page.

    Returns:
        Markdown string.

    Raises:
        requests.HTTPError / requests.RequestException on network failures.
    """
    options = {
        "inline_title": inline_title,
        "ignore_links": ignore_links,
        "use_readability": use_readability,
    }

    if url.startswith(APPLE_DEV_PREFIX):
        return fetch_apple_dev_doc(url, options)
    elif url.startswith(STACKOVERFLOW_PREFIX):
        return process_stackoverflow(url, options)
    else:
        html = fetch_url(url)
        return process_html(html, url=url, options=options)


# ===========================================================================
# Test block
# ===========================================================================

if __name__ == "__main__":
    test_url = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "https://en.wikipedia.org/wiki/Python_(programming_language)"
    )

    print(f"Converting: {test_url}")
    print("=" * 60)

    try:
        result = url_to_markdown(
            test_url,
            inline_title=True,
            ignore_links=False,
            use_readability=True,
        )
        print(result)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
