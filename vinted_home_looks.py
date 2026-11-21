"""Garment construction from the buying workbook for personalised Home discoveries."""

from functools import lru_cache
import re

from vinted_scraper_styles import alternatives, words


# Each row needs every group. A garment feature can appear in the title and
# another in the seller's description. Hashtags and styling suggestions are excluded.
LOOKS = {
    "utility_jacket": (
        ("jacket|bomber", "funnel|stand collar|standing collar|high collar|double collar", "shaped|cinched|fitted|ribbed hem|waist tabs|belted|buckle|double zip"),
        ("jacket|bomber", "cargo pockets|utility pockets|patch pockets|military pockets", "cropped|cinched|fitted|adjustable waist|drawstring waist"),
    ),
    "leather_jacket": (
        ("jacket|biker|bomber", "leather|leatherette|suede", "racer|motocross|asymmetric zip|off centre zip|double zip|buckled waist|buckle detailing|ribbed waistband|tight fitting"),
    ),
    "peacoat": (
        ("coat|peacoat|jacket", "double breasted|two rows of buttons", "cropped|hip length|waist length|shaped waist|fitted waist|swing"),
    ),
    "afghan_coat": (
        ("coat|jacket", "suede|sheepskin|embroidered", "shaggy collar|fur cuffs|furry cuffs|mongolian trim|fur edging"),
    ),
    "mesh_top": (
        ("top|blouse|tee", "mesh|sheer", "long sleeve|long sleeves|long sleeved", "baby pink|bubblegum pink|grape|polka dot|contrast stitching|flocked|fitted|second skin"),
        ("top|tee|blouse", "mesh|sheer", "tattoo|placement print|flocked pattern|polka dot|face print|tribal print"),
    ),
    "cowl_top": (
        ("top|blouse|halter", "cowl|draped neckline|waterfall neck|keyhole|key hole", "beaded|embellished|slinky|ruched|asymmetric|halter neck|halter ties"),
    ),
    "lace_cami": (
        ("cami|camisole|strappy|vest|top", "empire line|empire waist|babydoll|underbust seam|boning", "lace|ribbon|satin|embroidery|embroidered"),
        ("cami|camisole|strappy vest|spaghetti straps", "lace|crochet lace", "scalloped|bow|floral|satin|ruffled|underbust"),
    ),
    "zip_knit": (
        ("knit|knitted|jumper|cardigan|hoodie", "zip through|zip front|full zip|double zip|two way zip|dual zip", "cable|cabled|striped|stripy|fairisle|fair isle|fitted"),
    ),
    "fine_cardigan": (
        ("cardigan|shrug|bolero|wrap knit", "ribbon bow|ribbon ties|tie front|wrap front|crochet|pointelle|eyelet knit", "cropped|short|fitted|ruffle|ruffled|open front|bow|scalloped|wrap"),
    ),
    "shaped_skirt": (
        ("skirt|skort", "midi|mid length|calf length", "drop waist|tie waist|handkerchief|patchwork|bias cut|pleated wrap|pleated fitted|fitted pleated"),
    ),
    "shaped_trousers": (
        ("trousers|pants|capris", "pinstripe|pinstriped|pin stripe|cargo pockets", "flare|flared|bootcut|boot cut|capri|cropped|low waist"),
    ),
    "shaped_dress": (
        ("dress", "slinky|mesh|sheer|jersey", "asymmetric|asymmetrical|handkerchief|seam detail|face print|layered|bias cut"),
    ),
    "layered_henley": (
        ("henley|button neck|button placket|button neckline", "top|tee|jersey|long sleeve|long sleeved", "layered|double layer|mock layer|attached vest|with vest|with a vest|waffle knit|thermal rib"),
        ("henley|button neck|button placket", "fitted|slim fit|waist shaped", "contrast trim|lace edging|ruffled placket|ribbed|waffle"),
    ),
    "fur_hood_bomber": (
        ("jacket|bomber|parka|puffer|hoodie", "fur hood|fur trimmed hood|faux fur hood|fur on the hood|furry hood|fur lined hood", "fitted|cinched waist|waist tabs|ribbed waistband|ribbed hem|cropped|belted"),
    ),
    "beaded_evening_top": (
        ("cami|camisole|halter|halterneck|spaghetti straps", "beaded|beading|embroidered|embroidery|sequinned|sequined|sequin", "satin|silk|chiffon|bias cut|asymmetric|tie back|scalloped|hand beaded"),
    ),
    "foldover_flares": (
        ("trousers|pants|bottoms|leggings|flares", "fold over|foldover|fold down|turn down waistband", "flared|flare|flares|bootcut|boot cut|bell bottom", "contrast waistband|embroidered|floral waistband|printed waistband|rhinestone|logo band|velour|old label|2000s"),
    ),
    "ralph_cable": (
        ("cable|cabled|cableknit|aran", "cardigan|cardi", "button front|button through|button down|buttons down|button up|full button|full placket|buttons all the way"),
    ),
    "ralph_zip": (
        ("pinstripe|pinstriped|pin stripe|pin striped", "jacket|fitted top|blazer", "zip|button|buttons|buttoned"),
        ("knit|knitted|jumper|cardigan|cable|ribbed", "zip through|zip up|zip front|full zip|full length zip|zip all the way|two way zip|double zip", "knit|knitted|cable|ribbed|fitted|cropped"),
    ),
    "ralph_feminine": (
        ("pink|blush|rose", "knit|knitted|cable|jumper|cardigan", "fitted|slim fit|cable|pointelle|peplum|baby pink|pale pink|pastel pink|light pink"),
    ),
}
RALPH_CATEGORIES = {
    "ralph_cable": {"194", "13", "12"},
    "ralph_zip": {"1037", "12", "13", "194", "196"},
    "ralph_feminine": {"13", "194", "12", "196"},
}
_CUT_TEXT = re.compile(r"#|\b(?:styled with|paired with|search tags|tags\s*:|keywords\s*:)", re.I)
_NEGATIONS = {"not", "no", "without", "non", "never"}
_LARGER_TITLE = re.compile(r"\b(?:size|tagged|labelled)\s+(?:uk\s*)?(?:xl|xxl|xxxl|l|large|16|18|20)\b", re.I)
_PART_ZIP = re.compile(r"\b(?:quarter|half|partial)\s*[- ]?zip\b|[¼½]|\b1\s*/\s*[24]\s*[- ]?zip\b", re.I)


def partial_zip(title, description):
    return any(_PART_ZIP.search(_CUT_TEXT.split(value, maxsplit=1)[0]) for value in (title, description))


def contradicts_small_size(title):
    return bool(_LARGER_TITLE.search(title))


@lru_cache(maxsize=160)
def matching_looks(title, description):
    fields = []
    for value in (title, description):
        garment = _CUT_TEXT.split(value[:2400], maxsplit=1)[0]
        fields.extend(words(part) for part in re.split(r"[.!?;\n]", garment) if part.strip())
    tokens = {}
    for field in fields:
        for pos, token in enumerate(field):
            if not _NEGATIONS.intersection(field[max(0, pos-3):pos]):
                tokens.setdefault(token, []).append(field[pos:pos+5])

    def has(group):
        return any(candidate[:len(term)] == term for term in alternatives(group)
                   for candidate in tokens.get(term[0], ()))

    found = {key for key, rows in LOOKS.items()
             if any(all(has(group) for group in row) for row in rows)}
    if partial_zip(title, description):
        found.difference_update({"ralph_zip", "zip_knit"})
    return frozenset(found)


def ralph_guide(terms, structural):
    if not structural.get("brand_id", set()).intersection({"88", "4273"}):
        return None
    terms = {v.casefold().strip() for v in terms}
    if len(terms & {"cable", "cabled", "cableknit", "cableknitted", "aran"}) >= 3:
        return "ralph_cable"
    if len(terms & {"pinstripe", "pinstriped", "pin stripe", "pin striped", "striped zip"}) >= 3:
        return "ralph_zip"
    if len(terms & {"baby pink", "pale pink", "light pink", "pink cable", "pink knit", "peplum"}) >= 3:
        return "ralph_feminine"
    return None
