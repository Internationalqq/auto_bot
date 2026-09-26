"""Small authenticated preview cache. Only persisted supplier metadata can be read."""
from functools import lru_cache
from autobot.buyer_discovery import fetch_public
from autobot.hermes_buyer import BuyerError


@lru_cache(maxsize=24)
def thumbnail(url, hour):
    try:
        _, body, mime = fetch_public(url, image=True)
        valid = (mime == 'image/jpeg' and body.startswith(b'\xff\xd8\xff') or
                 mime == 'image/png' and body.startswith(b'\x89PNG\r\n\x1a\n') or
                 mime == 'image/gif' and body[:6] in (b'GIF87a', b'GIF89a') or
                 mime == 'image/webp' and body[:4] == b'RIFF' and body[8:12] == b'WEBP')
        if not valid: return None
        return body, mime
    except (BuyerError, OSError):
        return None
