"""Fixed anonymous X embed read for approval evidence; no browser or credentials."""
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import re

from host.tools import JSONObject
from host.tools.shared.web import json_request


class _TweetText(HTMLParser):
    """Only the embed's post paragraph, excluding the author/date footer."""
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_quote = False
        self.in_text = False
        self.text_closed = False
        self.quote_closed = False
        self.paragraphs = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "blockquote" and self.in_quote:
            raise ValueError("Nested embed quote")
        if tag == "blockquote" and "twitter-tweet" in (dict(attrs).get("class") or "").split():
            if self.quote_closed:
                raise ValueError("Ambiguous embed quote")
            self.in_quote = True
        elif tag == "p" and self.in_quote:
            self.paragraphs += 1
            self.in_text = True
        elif tag == "br" and self.in_text:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self.in_text:
            self.text_closed = True
            self.in_text = False
        elif tag == "blockquote" and self.in_quote:
            self.quote_closed = not self.in_text
            self.in_quote = self.in_text = False

    def handle_data(self, data: str) -> None:
        if self.in_text:
            self.parts.append(data)


def target_tweet(tweet_id: str) -> JSONObject:
    if not re.fullmatch(r"[0-9]{1,25}", tweet_id):
        raise ValueError("Reply target must be a numeric X post ID.")
    context: JSONObject = {
        "id": tweet_id, "url": f"https://x.com/i/status/{tweet_id}",
        "source": "X public oEmbed", "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "content_scope": "Public embed text; may be cached or shortened by X. Excludes media and linked pages.",
        "status": "unavailable",
    }
    try:
        # Only the fixed provider endpoint and a validated numeric ID leave
        # the host. No free-text guard, credentials, or redirects are needed.
        payload = json_request("GET",
            f"https://publish.x.com/oembed?url=https://x.com/i/status/{tweet_id}&omit_script=true",
            failure_message="X public embed could not be loaded.",
            invalid_response_message="X public embed returned invalid JSON.",
            timeout=20, max_bytes=100000)
        url = payload.get("url")
        html = payload.get("html")
        if not isinstance(url, str) or not isinstance(html, str):
            raise ValueError("Embed URL and HTML must be strings")
        match = re.fullmatch(r"https://(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/" + tweet_id, url)
        if match is None:
            raise ValueError("Embed does not identify the requested post")
        parser = _TweetText()
        parser.feed(html)
        parser.close()
        text = "".join(parser.parts)
        # Bound serialized UTF-8, including JSON escaping, leaving room for
        # account/proposal metadata within the host's 64 KiB payload limit.
        text_bytes = len(json.dumps(text, ensure_ascii=False).encode("utf-8"))
        if (parser.paragraphs != 1 or not parser.text_closed or not parser.quote_closed
                or not text.strip() or text_bytes > 16000):
            raise ValueError("Missing, ambiguous or oversized embed text")
        context.update(status="loaded", text=text, author_username=match[1], url=url)
    except Exception:
        context["error"] = "X public embed did not provide usable text for the requested post. Target text is unavailable for approval review."
    return context
