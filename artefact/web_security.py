"""Browser request origin checks shared by the local HTTP interfaces."""
from urllib.parse import urlsplit


def same_origin(headers):
    """Check browser origin evidence after the caller validates the local Host.

    A missing/opaque Origin is accepted only with an exact local HTTP Referer.
    Authentication and the per-server CSRF token remain required by callers.
    Contradictory fetch metadata or duplicate security headers fail closed.
    """
    if hasattr(headers, 'get_all'):
        for name in ('Host', 'Origin', 'Referer', 'Sec-Fetch-Site'):
            if len(headers.get_all(name, [])) > 1:
                return False
    host = headers.get('Host', '')
    if not host:
        return False
    if headers.get('Sec-Fetch-Site') not in (None, 'same-origin'):
        return False
    origin = headers.get('Origin')
    if origin not in (None, 'null', 'http://' + host):
        return False
    referer = headers.get('Referer')
    if referer is not None:
        # urlsplit silently removes these characters; do not normalize bad input.
        if any(char in referer for char in '\r\n\t'):
            return False
        try:
            parsed = urlsplit(referer)
        except ValueError:
            return False
        if parsed.scheme != 'http' or parsed.netloc != host:
            return False
    return origin == 'http://' + host or referer is not None
