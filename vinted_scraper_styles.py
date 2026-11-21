"""Style evidence for the owner's brand-independent women's searches.

Each row describes one garment family from the scraper. Search names and IDs
are editable: opt in through the saved category and at least two distinctive
keyword alternatives, so deleting a search also removes its Home coverage.
"""
import re
import unicodedata
from functools import lru_cache
from typing import NamedTuple


class Style(NamedTuple):
    categories: tuple[str, ...]
    anchors: tuple[str, ...]
    # Each tuple requires every feature. Vertical bars list interchangeable terms.
    looks: tuple[tuple[str, ...], ...]


STYLES = {
    "utility_jacket": Style(("1037",), ("funnel neck", "cargo jacket", "high neck bomber"), (
        ("funnel|funnelneck|stand collar|stand up collar",),
        ("high neck|high collar", "bomber|zip up|zip front|cropped|fitted"),
        ("cargo|utility|military|multi pocket|multipocket", "jacket|bomber|zip front|cropped"),
    )),
    "leather_jacket": Style(("1037",), ("leather bomber", "leather biker", "fitted leather"), (
        ("leather|leatherette|suede", "biker|racer|motocross|moto|bomber|fitted|ribbed hem|buckle"),
    )),
    "peacoat": Style(("1037",), ("peacoat", "pea coat"), (
        ("peacoat|peacoats|pea coat|pea coats|pea jacket",),
        ("double breasted", "short coat|cropped coat|fitted coat|wool jacket|swing coat"),
    )),
    "afghan_coat": Style(("1037",), ("afghan coat", "penny lane"), (
        ("afghan|penny lane|pennylane",),
        ("fur trim|faux fur trim|shearling trim|shaggy collar|sheepskin trim", "suede|embroidered|bohemian|boho"),
    )),
    "mesh_top": Style(("12",), ("mesh top", "sheer mesh", "tattoo print"), (
        ("mesh|semi sheer|sheer", "printed|graphic|lace|long sleeve|long sleeved|fitted|floral|ruched|asymmetric|top|blouse"),
        ("tattoo|tattoo print|tattoo style|tribal print", "top|tee|mesh|fitted|sleeve"),
    )),
    "cowl_top": Style(("12",), ("cowl neck", "keyhole top", "slinky jersey"), (
        ("cowl|waterfall neckline|draped neckline|keyhole|key hole",),
        ("slinky", "jersey|draped|fitted|asymmetric|asymmetrical|halter|halterneck"),
    )),
    "lace_cami": Style(("12",), ("babydoll", "lace cami", "corset top"), (
        ("babydoll|baby doll|bustier|corset|milkmaid",),
        ("lace|lacy|lace trimmed|lace trim", "cami|camisole|strappy top|vest top|halter"),
    )),
    "zip_knit": Style(("13","194","196"), ("knit zip", "zip cardigan", "striped hoodie"), (
        ("knit|knitted|knitwear|cardigan|cable|ribbed", "zip|zipped|zipper|zipup|fullzip"),
        ("striped|stripe|stripes|stripey|stripy", "hoodie|hoody|hooded|zip through|zip up"),
    )),
    "fine_cardigan": Style(("13","194"), ("pointelle", "wrap cardigan", "tie front bolero"), (
        ("pointelle|openwork knit|open work knit",),
        ("wrap|tie front|tie up|tie fastening|ribbon tie|ballet tie", "cardigan|bolero|shrug|knit|knitted"),
    )),
    "shaped_skirt": Style(("11",), ("low rise midi", "tiered midi", "asymmetric midi"), (
        ("low rise|low waist|low waisted|low slung|tiered|asymmetric|asymmetrical|bias cut|pinstripe|pinstriped", "midi|mid length|maxi"),
        ("hanky hem|handkerchief hem|hankie hem|mesh overlay",),
    )),
    "shaped_trousers": Style(("9",), ("pinstripe flare", "flared suit", "cargo capri"), (
        ("pinstripe|pinstriped|pin stripe|pin striped|suit|tailored", "flare|flares|flared|bootcut|boot cut|wide leg|low rise"),
        ("cargo|utility|multi pocket|multipocket", "capri|capris|cropped|three quarter"),
    )),
    "shaped_dress": Style(("10",), ("mesh dress", "asymmetric hem", "slinky jersey"), (
        ("mesh|sheer", "print|printed|layered|dress|floral|graphic|asymmetric|asymmetrical"),
        ("asymmetric hem|asymmetrical hem|handkerchief hem|hanky hem|hankie hem",),
        ("slinky", "jersey|layered|asymmetric|asymmetrical|draped|bias cut"),
    )),
}


def words(text):
    return tuple(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold()))


@lru_cache(maxsize=300)
def alternatives(feature):
    return tuple(words(phrase) for phrase in feature.split("|"))


def evidence(text, profile):
    """Require nearby seller-described features and ignore tag lists and negations."""
    # Hair, another item in an outfit, and seller tags cannot complete a garment feature pair.
    text = re.split(r"#|\b(?:styled with|paired with|search tags|tags\s*:|keywords\s*:)", text[:6000], maxsplit=1, flags=re.I)[0]
    for fragment in re.split(r"[.!?;\n]", text)[:40]:
        tokens = words(fragment)
        if not tokens:
            continue
        for look in profile.looks:
            positions = []
            for feature in look:
                found = [
                    at for needle in alternatives(feature)
                    for at in range(len(tokens) - len(needle) + 1)
                    if tokens[at:at + len(needle)] == needle
                    and not {"not", "no", "without", "non", "never"}.intersection(tokens[max(0, at-2):at])
                ]
                if not found:
                    break
                positions.append(found)
            if len(positions) == len(look) and (
                len(positions) == 1 or any(abs(a-b) <= 10 for a in positions[0] for b in positions[1])
            ):
                return True
    return False


def profile_for(search_terms, filters):
    if "brand_id" in filters:
        return None
    scope = filters.get("category_ids", set())
    terms = {term.strip().casefold() for term in search_terms}
    for key, profile in STYLES.items():
        if scope and scope <= set(profile.categories) and len(terms.intersection(profile.anchors)) >= 2:
            return key
    return None

# Specific construction required on open-label searches, in addition to a relevant
# garment. The scraper contains older high-street pieces with distinctive cuts.
_CONSTRUCTION = {
    "utility_jacket": ("ribbed hem|elasticated waist|cropped|fitted|multi pocket|multipocket|epaulettes|asymmetric|buckle|bomber|military",),
    "leather_jacket": ("fitted|cropped|buckle|belted|racer|ribbed|bomber|asymmetric",),
    "peacoat": ("double breasted|wool|swing|fitted|cropped|pleated|military|chunky button",),
    "afghan_coat": ("suede|shearling|embroidered|fur trim|fur collar|mongolian|mongolian fur|shaggy",),
    "mesh_top": ("tattoo|graphic|printed|print|floral|ruched|corset|embroidery|layered|asymmetric|beaded|contrast trim",),
    "cowl_top": ("beaded|asymmetric|ruched|gathered|printed|lace|keyhole|wrap|halter|embellished|draped",),
    "lace_cami": ("empire|butterfly|beaded|boned|boning|panelled|lace trim|polka dot|asymmetric|floral|ribbon|embroidered|buckle|tiered|satin|lace up",),
    "zip_knit": ("cable|cabled|ribbed|fitted|cropped|stripe|striped|stripes|funnel|embroidery",),
    "fine_cardigan": ("pointelle|ballet|bolero|shrug|ribbon|wrap cardigan|cropped|eyelet",),
    "shaped_skirt": ("tiered|bias cut|handkerchief|hanky|asymmetric|asymmetrical|mesh overlay|ruched",),
    "shaped_trousers": ("pinstripe|pinstriped|pin stripe|cargo capri|cargo capris|multi pocket|side pockets",),
    "shaped_dress": ("asymmetric|asymmetrical|layered|handkerchief|hanky|ruched|graphic|beaded|floral print",),
}
_DISCOUNT = ("shein","romwe","temu","primark","boohoo","pretty little thing","prettylittlething",
             "fashion nova","missguided","nasty gal","cider")
_HIGH_STREET = ("zara","h m","hm","new look","bershka","stradivarius","pull bear","pull and bear",
                "asos","next","debenhams","atmosphere","topshop","george","george at asda","ovs","forever 21")
_PERIOD = r"\b(?:old label|original label|made in (?:the )?(?:90s|00s|1990s|2000s)|1990s|2000s|early 00s|late 90s|90s vintage|00s vintage)\b"


def named_profile(search):
    words = search.get("shared_keywords") or (search.get("ebay") or {}).get("shared_keywords") or []
    if not words or not search.get("query"):
        return None
    from vinted_home_matching import _filters
    parsed = _filters(search["query"])
    return profile_for(words, parsed[1]) if parsed else None


def garment_quality(profile, title, description, brand=""):
    """Return a hard rejection or whether the seller supplied distinctive features."""
    name = " " + " ".join(words(brand or "") + words(title or "")) + " "
    if any(" " + x + " " in name for x in _DISCOUNT):
        return "blocked"
    high_street = any(" " + x + " " in name for x in _HIGH_STREET)
    fields = [str(f or "")[:6000] for f in (title, description)]
    fields = [re.split(r"#|\b(?:styled with|paired with|search tags|tags\s*:|keywords\s*:)", f, maxsplit=1, flags=re.I)[0] for f in fields]
    if high_street and not any(re.search(_PERIOD, f, flags=re.I) for f in fields):
        return "weak_high_street"
    detail = Style((), (), tuple((x,) for x in _CONSTRUCTION[profile]))
    if any(evidence(f, detail) for f in fields):
        return "distinctive"
    return "weak_high_street" if high_street else "unconfirmed"
