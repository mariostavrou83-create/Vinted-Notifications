"""Public garment details that distinguish the closest asking prices from weaker comparisons."""

import re

from resale_pricing import garment

# Keep only observable construction, pattern and material terms. Different preferences
# for an era/colour are not a claim that one garment is worth more.
FEATURES = {
    "fit": {"fitted": r"fitted|slim fit", "oversized": r"oversized|oversize"},
    "length": {"cropped": r"cropped|crop", "maxi": r"maxi|full length", "midi": r"midi"},
    "opening": {"zip front": r"full zip|zip up|zip front", "pullover": r"pullover|pull over"},
    "outer construction": {"puffer": r"puffers?|padded (?:jackets?|coats?|outerwear)", "trench": r"trench", "blazer": r"blazers?"},
    "detail": {"hooded": r"hooded", "belted": r"belted", "strapless": r"strapless",
               "asymmetric": r"asymmetr(?:ic|ical)", "pleated": r"pleated", "ruched": r"ruched",
               "lace trim": r"lace trim(?:med)?"},
    "pattern": {"argyle": r"argyle|diamond pattern", "plaid": r"plaid|tartan", "pinstripe": r"pinstrip(?:e|ed)"},
    "finish": {"cable knit": r"cable knit(?:ted)?", "beading": r"beaded|beading",
               "sequins": r"sequin(?:s|ned)?", "embroidery": r"embroidered|embroidery"},
    "trim": {"fur collar": r"fur collar(?:ed)?", "fur hood": r"fur hood(?:ed)?"},
    "fabric": {"suede": r"suede", "denim": r"denim", "cashmere": r"cashmere",
               "silk": r"silk", "leather": r"leather"},
}
PATTERNS = {
    family: {name: re.compile(r"\b(?:" + pattern + r")\b") for name, pattern in labels.items()}
    for family, labels in FEATURES.items()
}
WEAR = {
    "peeling": re.compile(r"\b(?:peeling|peeling off|peels? off)\b", re.I),
    "staining": re.compile(r"\b(?:stained|stains?|staining)\b", re.I),
    "missing button": re.compile(r"\b(?:missing (?:a |one )?button|button (?:is )?missing)\b", re.I),
}


def reported_wear(description):
    # Store named wear descriptors only, never the seller's free-text description.
    if not isinstance(description, str):
        return []
    defects = set()
    clauses = re.split(r"[.;!?\n]|\b(?:but|except)\b", description[:2000], flags=re.I)
    for clause in clauses:
        if re.search(r"\b(?:no|not|never|without|free of|no signs of|resistant|proof)\b", clause, re.I):
            continue
        defects.update(name for name, expression in WEAR.items() if expression.search(clause))
    return sorted(defects)


def _features(item):
    text = " ".join(str(item.get(k) or "") for k in ("title", "material", "subtype", "leaf_title", "category")).casefold()
    supplied = item.get("visible_traits")
    if isinstance(supplied, list):
        # Only words on the controlled list can be used as construction evidence.
        allowed = {term for labels in FEATURES.values() for term in labels}
        text += " " + " ".join(t for t in supplied if isinstance(t, str) and t in allowed)
    text = re.sub(r"[-–—]", " ", text)
    return {group: {name for name, pattern in terms.items() if pattern.search(text)}
            for group, terms in PATTERNS.items()}


def fully_described_match(target, other):
    return (not garment(target.get("title")) or not garment(other.get("title")) or
            _features(target) == _features(other))


_SET = re.compile(r"\b(skirt suit|trouser suit|pant suit|two piece|2 piece|co ord|matching set|twinset)\b")
_SKIRT = re.compile(r"\bskirts?\b")
_CONFLICTING_SKIRT_TYPES = re.compile(r"\b(?:jackets?|blazers?|coats?|dresses|dress|trousers|jeans|tops?)\b")


def _label_family(value):
    from resale_pricing import words
    text = words(value)
    if not text:
        return ""
    if _SET.search(text) or (_SKIRT.search(text) and _CONFLICTING_SKIRT_TYPES.search(text)):
        return "set"
    if re.search(r"\bskorts?\b", text):
        return "skort"
    if re.search(r"\b(?:t shirt|tee shirt|tshirt)s?\b", text):
        return "top"
    if re.search(r"\bpuffers?\b", text) and not garment(text):
        return "jacket"
    for identity, pattern in (("bag", r"bags?|handbags?"), ("shoes", r"shoes?|trainers?|boots?")):
        if re.search(r"\b(?:" + pattern + r")\b", text):
            return identity
    return garment(text) or ""


def family_facts(item):
    # The original item title and its own leaf category describe the sale listing.
    # A visual prediction can contradict these public facts but cannot replace them.
    title = _label_family(item.get("title"))
    leaf = _label_family(item.get("leaf_title") or item.get("category"))
    photo = str(item.get("photo_family") or "").casefold()
    if not photo and "puffer" in (item.get("visible_traits") or []):
        photo = "jacket"
    supplied = {x for x in (title, leaf, photo) if x}
    if "set" in supplied and len(supplied) > 1:
        return "set", True
    if len(supplied) > 1:
        outer = {title, leaf, photo} - {""}
        label = str(item.get("leaf_title") or item.get("category") or "") + " " + str(item.get("title") or "")
        if not (outer <= {"coat", "jacket"} and re.search(r"\b(?:puffers?|padded)\b", label, re.I)):
            return title or leaf, True
    return leaf or title, False


def same_garment_family(target, other):
    kind_a, conflict_a = family_facts(target)
    kind_b, conflict_b = family_facts(other)
    if conflict_a or conflict_b or not kind_a or not kind_b:
        return False
    if kind_a == kind_b:
        return True
    if {kind_a, kind_b} <= {"jacket", "coat"}:
        leaf = target.get("vinted_catalog_id")
        if leaf and leaf == other.get("vinted_catalog_id"):
            return True
        return bool(_features(target)["outer construction"] & _features(other)["outer construction"])
    return False


def family_mismatch(target, other):
    a, conflict_a = family_facts(target)
    b, conflict_b = family_facts(other)
    if conflict_a or conflict_b:
        return "Conflicting listing title, category or photo"
    if (a or b) and not same_garment_family(target, other):
        return "Different or unconfirmed product type"
    return None


def _distinctive_shared(a, b):
    return set().union(*(a[group] & b[group] for group in (
        "outer construction", "pattern", "finish", "trim"))) | (
            a["detail"] & b["detail"] & {"lace trim", "ruched", "asymmetric", "strapless", "pleated"})


def strong_style_match(target, other):
    """An affirmative rescue for a sparse title, never plain colour, fit or an unexplained size."""
    from trial_price_rules import field
    if not same_garment_family(target, other) or not all(
        field(target, k) and field(target, k) == field(other, k) for k in ("brand", "size")
    ):
        return False
    return bool(_distinctive_shared(_features(target), _features(other)))


def assess(target, other, target_grade, other_grade):
    """Compare verified features first. A missing description cannot establish inferior quality."""
    if not garment(target.get("title")) or not garment(other.get("title")):
        return "close", "Same stated condition" if target_grade == other_grade else "Higher stated condition"
    target_traits, other_traits = _features(target), _features(other)
    for group in ("fit", "length", "opening", "outer construction", "fabric", "pattern"):
        left, right = target_traits[group], other_traits[group]
        if left and right and left.isdisjoint(right):
            return "different", "Different " + group + ": " + ", ".join(sorted(left | right))
    a = set().union(*target_traits.values())
    b = set().union(*other_traits.values())
    shared = ", ".join(sorted(a & b)[:4])
    reason = "Both list " + shared + ". " if shared else ""
    if target_grade != other_grade:
        relation = "lower" if other_grade < target_grade else "higher"
        return "context", reason + "Comparison has a " + relation + " stated condition."
    target_wear = set(target.get("reported_wear") or [])
    other_wear = set(other.get("reported_wear") or [])
    if target_wear != other_wear:
        target_note = ", ".join(sorted(target_wear - other_wear))
        other_note = ", ".join(sorted(other_wear - target_wear))
        return "context", reason + (("Target discloses " + target_note + ". ") if target_note else "") + (
            ("Comparison discloses " + other_note + ".") if other_note else "").rstrip()
    if a != b:
        left, right = ", ".join(sorted(a - b)[:3]), ", ".join(sorted(b - a)[:3])
        tier = "close" if strong_style_match(target, other) else "context"
        return tier, reason + (("Unconfirmed on comparison: " + left + ". ") if left else "") + (
            ("Unconfirmed on target: " + right + ".") if right else "")
    return "close", reason + "Same stated condition."
