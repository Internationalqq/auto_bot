"""Read ordinary search-result links; a snippet is never price evidence."""
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup


def google_result_links(page_html: str, *, limit: int = 20) -> list[dict[str, str]]:
    soup = BeautifulSoup(page_html or '', 'html.parser')
    results, seen = [], set()
    for heading in soup.select('h3'):
        anchor = heading.find_parent('a', href=True)
        if anchor is None:
            continue
        url = urljoin('https://www.google.com', str(anchor.get('href') or ''))
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or '').casefold()
            if host in {'google.com', 'www.google.com'} and parsed.path == '/url':
                params = parse_qs(parsed.query)
                url = (params.get('q') or params.get('url') or [''])[0]
                parsed = urlparse(url)
                host = (parsed.hostname or '').casefold()
            if (parsed.scheme not in {'https', 'http'} or not host or parsed.username
                    or host in {'google.com', 'www.google.com'} or host.endswith('.google.com')):
                continue
        except ValueError:
            continue
        url = parsed._replace(fragment='').geturl()
        if url in seen:
            continue
        seen.add(url)
        title = heading.get_text(' ', strip=True)
        # Bound the enclosing text to one result; do not mix a neighbouring
        # seller's price with this link. Changing markup can only reduce recall.
        snippet = title
        node = anchor
        for _ in range(4):
            parent = node.parent
            if parent is None or len(parent.select('h3')) != 1:
                break
            text = parent.get_text(' ', strip=True)
            if len(text) > 1200:
                break
            snippet = text
            node = parent
        results.append({'title': title[:500], 'url': url, 'snippet': snippet[:1000]})
        if len(results) >= max(1, min(40, limit)):
            break
    return results
