"""Literal positive-keyword evidence for the new shared alert format.

Marketplace search relevance is candidate discovery, not proof that a saved
alternative occurs in seller text. This helper performs no network or state
changes; callers must obtain and bind any necessary description separately.
"""

from listing_text import TextParser
from search_settings import excluded_by

MAX_TEXT = 100000
SHORTENED = "\n[Description shortened — see listing for the rest.]"


def required_keywords(search):
    """Legacy alerts and shared alerts without alternatives keep their rules."""
    config = search.get("ebay") or search
    version = search.get("shared_alert_version", config.get("shared_alert_version"))
    if version != 1:
        return []
    words = search.get("shared_keywords", config.get("shared_keywords", []))
    return words if isinstance(words, list) else []


def description_text(value, *, html=False):
    """Return bounded seller text and whether truncation left it incomplete."""
    if not isinstance(value, str):
        return "", False
    complete = len(value) <= MAX_TEXT
    value = value[:MAX_TEXT]
    if value.endswith(SHORTENED):
        value, complete = value[: -len(SHORTENED)], False
    if html:
        parser = TextParser()
        parser.feed(value)
        value = "".join(parser.parts)
    return value, complete


def keyword_evidence(
    search,
    title,
    description="",
    *,
    description_complete=False,
    description_html=False,
):
    """Require one alternative within either title or seller description.

    An unread description is not a confirmed mismatch. Positive evidence needs
    only the matching text; negative evidence needs a complete description.
    Title and description are checked separately so a phrase never spans them.
    """
    words = required_keywords(search)
    result = {"state": "unrestricted", "keyword": None, "field": None}
    if not words:
        return result
    title = title[:MAX_TEXT] if isinstance(title, str) else ""
    found = excluded_by(title, words)
    if found:
        return {"state": "matched", "keyword": found, "field": "title"}
    text, complete = description_text(description, html=description_html)
    found = excluded_by(text, words)
    if found:
        return {"state": "matched", "keyword": found, "field": "description"}
    result["state"] = "absent" if description_complete and complete else "unverified"
    return result


def matches_keywords(search, title, description="", **options):
    """A boolean gate; unresolved evidence must never qualify an alert."""
    return keyword_evidence(search, title, description, **options)["state"] in {
        "unrestricted",
        "matched",
    }
