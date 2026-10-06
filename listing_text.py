"""Render seller descriptions as text, never executable seller HTML."""

import re
from html.parser import HTMLParser


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "iframe", "template"):
            self.hidden.append(tag)
        if not self.hidden and tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag in ("p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def clean_description(value, *, html=False):
    if not isinstance(value, str):
        return ""
    value = value[:100000]
    if html:
        parser = TextParser()
        parser.feed(value)
        value = "".join(parser.parts)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    value = "\n".join(
        re.sub(r"[ \t\r]+", " ", line).strip() for line in value.splitlines()
    )
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    return (
        value
        if len(value) <= 6000
        else value[:6000] + "\n[Description shortened — see listing for the rest.]"
    )
