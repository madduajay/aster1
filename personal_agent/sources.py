"""General first-party source policy. No entity-to-domain lookup table."""
import ipaddress
import json
import re
from html import unescape
from urllib.parse import unquote_plus, urlsplit, urlunsplit


POLICY = (
    "Use authentic PRIMARY, FIRST-PARTY sources only for external factual research. Identify the entity or "
    "entities in the current request and determine their genuine official domains dynamically. A company's "
    "products, fees, policies and offers must come from that company's own website; government facts from "
    "the responsible agency, and research from its original publisher or institution. Popularity or a high "
    "search rank does not make a review, comparison, affiliate, aggregator, wiki, forum or news article the "
    "official source for another entity. Example only: an American Express/Amex product question uses "
    "americanexpress.com and its genuine subdomains, not card-review or comparison sites. Apply this same "
    "principle to every subject; the example is not a fixed domain rule. Pass only the relevant official "
    "domains to the tools. Do not expand the scope to a third party merely because the official site is "
    "unavailable or unhelpful. If you cannot confidently identify an official source, say so and ask for the "
    "official website instead of guessing. Obtain first-party evidence before answering external factual "
    "questions; do not fill evidence gaps from model memory, old assistant answers or session summaries. "
    "Cite only exact URLs returned by the current run's verified search/browser tools. Present results as "
    "descriptive clickable Markdown links wherever a verified URL is available: link the product, card, "
    "document, service or result title directly where it appears in the answer, instead of leaving names "
    "as plain text and putting all links only at the end. Prefer the exact verified detail page; if only "
    "an official overview page was verified, label that link clearly rather than inventing a detail URL. "
    "Do not invent links or use third-party URLs merely to make a result clickable. Explain missing "
    "official evidence plainly. If search returns official_page_leads without accepted evidence, read those "
    "official pages with the browser before giving up; do not cite unvisited leads. "
    "User personal facts and conversational requests do not need web research. "
)

URL = re.compile(r'https?://[^\s<>\[\]"`\u3010\u3011]+', re.I)
MARKDOWN_LINK = re.compile(r'\[([^\]\n]{1,300})\]\((https?://[^\s)]+)\)', re.I)
URL_END = '.,;:!?)}'


def normalize_domains(domains):
    if not domains or len(domains) > 8:
        raise ValueError('Provide 1–8 genuine official domains for the current subject; never guess or substitute third-party sites')
    result = []
    for value in domains:
        value = value.strip().lower().rstrip('.')
        if len(value) > 253 or not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', value):
            raise ValueError('Official domains must be hostnames without URLs, paths, ports, or wildcards')
        if any(part.startswith('-') or part.endswith('-') or len(part) > 63 for part in value.split('.')):
            raise ValueError('Invalid official domain')
        try:
            ipaddress.ip_address(value)
        except ValueError:
            pass
        else:
            raise ValueError('An IP address does not establish an official website')
        if value not in result:
            result.append(value)
    return result


def allowed_url(url, domains):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower().rstrip('.')
        return (parsed.scheme in {'http', 'https'} and not parsed.username and not parsed.password
                and parsed.port in {None, 80, 443}
                and any(host == domain or host.endswith('.' + domain) for domain in domains))
    except ValueError:
        return False


def urls(text):
    return [match.group().rstrip(URL_END) for match in URL.finditer(text)]


def canonical_url(url):
    """Compare evidence URLs without UTM attribution; keep functional query values intact.

    This is an identity key only. Displayed links always use the actual evidence URL.
    Paths, slashes, fragments, other query parameters and query order remain significant.
    """
    try:
        parsed = urlsplit(unescape(url))
        if parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            return None
        query = '&'.join(part for part in parsed.query.split('&')
                         if part and not unquote_plus(part.partition('=')[0]).lower().startswith('utm_'))
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, query, parsed.fragment))
    except ValueError:
        return None


def verified_overview(url, sources):
    """A strict ancestor page can be offered as an overview, never as a verified detail page."""
    target = canonical_url(url)
    if not target:
        return None
    parsed = urlsplit(target)
    candidates = []
    for source in sources:
        candidate = canonical_url(source['url'])
        if not candidate:
            continue
        parent = urlsplit(candidate)
        path = parent.path.rstrip('/')
        if (parent.scheme == parsed.scheme and parent.netloc == parsed.netloc and path
                and not parent.query and not parent.fragment and parsed.path.startswith(path + '/')):
            candidates.append(source)
    return max(candidates, key=lambda source: len(urlsplit(source['url']).path)) if candidates else None


def evidence_sources(db, run):
    sources = []
    for row in db.rows("SELECT payload FROM events WHERE run_id=? AND kind='sources' ORDER BY id", (run,)):
        sources.extend(json.loads(row['payload']))
    return list({item['url']: item for item in sources}.values())


def vetted_text(text, sources, *, empty_fallback=True):
    """Repair harmless citation differences and drop only lines containing unsupported links."""
    verified = {canonical_url(source['url'].rstrip(URL_END)): source['url'] for source in sources
                if canonical_url(source['url'].rstrip(URL_END))}

    def markdown(match):
        label, url = match.groups()
        exact = verified.get(canonical_url(url.rstrip(URL_END)))
        if exact:
            return f'[{label}]({exact})'
        overview = verified_overview(url, sources)
        if overview:
            return f"[{label} (official overview)]({overview['url']})"
        return match.group()

    text = MARKDOWN_LINK.sub(markdown, text)

    def restore_url(match):
        raw = match.group()
        url = raw.rstrip(URL_END)
        return verified.get(canonical_url(url), url) + raw[len(url):]

    text = URL.sub(restore_url, text)
    lines = [line for line in text.splitlines(keepends=True)
             if all(canonical_url(url) in verified for url in urls(line))]
    result = ''.join(lines)
    if result.strip() or not text.strip():
        return result
    if sources or not empty_fallback:
        return ''
    return "I couldn't find an official source for that detail."
