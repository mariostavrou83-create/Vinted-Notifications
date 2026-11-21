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
