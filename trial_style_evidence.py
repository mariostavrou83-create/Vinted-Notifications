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
_DAMAGE = r"(?:rip(?:s)?|tear(?:s)?|holes?)"
_SPECIFIC_DAMAGE = re.compile(
    rf"\b(?:a|one|some|has|visible|slight|small|tiny|large|minor|noticeable)\s+"
    rf"(?:(?:slight|small|little|tiny|large|minor|noticeable)\s+)?(?P<named>{_DAMAGE})\b"
    rf"|\b(?P<located>{_DAMAGE})\s+(?:at|in|on|along|near|under|beside|around|across|shown|visible|pictured)\b"
    r"|\b(?P<torn>ripped|torn)\s+(?:at|along|lining|cuff|collar|seam|pocket|sleeve|zipper|hem)\b",
    re.IGNORECASE,
)
WEAR = {
    "peeling": re.compile(r"\b(?:peeling|peels? off)\b", re.IGNORECASE),
    "staining": re.compile(r"\b(?:stained|stains?|staining)\b", re.IGNORECASE),
    "pilling": re.compile(r"\b(?:pilling|bobbling)\b", re.IGNORECASE),
    "scuffing": re.compile(r"\b(?:scuffed|scuffs|scuffing)\b", re.IGNORECASE),
    "damaged seam": re.compile(r"\b(?:loose|open|split) seam\b", re.IGNORECASE),
    "broken zip": re.compile(r"\b(?:broken|faulty) zip(?:per)?\b", re.IGNORECASE),
    "missing button": re.compile(r"\b(?:missing (?:a |one )?button|button (?:is )?missing)\b", re.IGNORECASE),
}
_NAMED_DEFECTS = {*WEAR, "rip or tear", "hole"}
_NEGATIVE = re.compile(r"\b(?:no|not|never|without|free (?:of|from)|hasn['’]?t|doesn['’]?t|isn['’]?t)\b", re.IGNORECASE)
_POSITIVE = re.compile(r"\b(?:there (?:is|are)|does have|has (?:a|one)|with (?:a|one)|"
                       r"(?:,|and)\s+(?:a|one|some slight|some small))\b", re.IGNORECASE)
_BY_DESIGN = re.compile(r"\b(?:intentional(?:ly)?|by design|deliberate(?:ly)?|decorative|designer rips)\b", re.IGNORECASE)


def _affirmed(clause, match):
    start = match.start("named") if "named" in match.re.groupindex and match.group("named") else match.start()
    before = clause[:start]
    # A previous denial need not apply to a newly introduced defect in the same sentence.
    negatives = list(_NEGATIVE.finditer(before))
    if negatives and not _POSITIVE.search(before, negatives[-1].end()):
        return False
    after = clause[match.end():]
    return not re.match(r"\s*[-–]?\s*(?:proof|resistant|free|resistance)\b", after, re.IGNORECASE)


def reported_wear(description):
    # Record named seller disclosures; ordinary distressed styling and absent prose are not proof of damage.
    if not isinstance(description, str):
        return []
    defects = set()
    clauses = re.split(r"[.;!?\n]|\b(?:but|except(?: for)?|apart from|however|although)\b",
                       description[:2000], flags=re.IGNORECASE)
    for clause in clauses:
        for name, pattern in WEAR.items():
            if any(_affirmed(clause, match) for match in pattern.finditer(clause)):
                defects.add(name)
        # A stated location or direct report distinguishes damage from a 'ripped jeans' style,
        # ordinary wear and tear, a thumb hole, or rip-resistant fabric.
        for match in _SPECIFIC_DAMAGE.finditer(clause):
            style_only = match.group("torn") and re.search(r"\b(?:distressed|fashionably|stylised)\b", clause, re.IGNORECASE)
            design_hole = re.search(r"\b(?:thumb|button|belt|eyelet)\s*$", clause[:match.start()], re.IGNORECASE)
            if _affirmed(clause, match) and not _BY_DESIGN.search(clause) and not style_only and not design_hole:
                named = (match.group("named") or match.group("located") or match.group("torn")).lower()
                defects.add("hole" if named.startswith("hole") else "rip or tear")
    return sorted(defects)


def known_wear(item):
    wear = item.get("reported_wear")
    return _NAMED_DEFECTS.intersection(wear) if isinstance(wear, (list, tuple, set)) else set()


def wear_difference(target, other):
    left, right = known_wear(target), known_wear(other)
    if left == right:
        return ""
    notes = []
    if left - right:
        notes.append("Target seller discloses " + ", ".join(sorted(left - right)) + ".")
    if right - left:
        notes.append("Comparison seller discloses " + ", ".join(sorted(right - left)) + ".")
    return " ".join(notes)



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
        if not (outer <= {"coat", "jacket"} and re.search(r"\b(?:puffers?|padded)\b", label, re.IGNORECASE)):
            return title or leaf, True
    return leaf or title, False


# UK adult clothing size bands and children's ages often use the very same numbers.
# Only an explicit age unit, department, or UK apparel band establishes an audience.
_AGE = re.compile(r"\b(?:age[ds]?\s+|size\s+)?(\d{1,2})(?:\s*(?:[-–—/]|to)\s*(\d{1,2}))?\s*"
                  r"(years?(?:\s+old)?|yrs?|y/?o|y|months?|mos?)\b", re.IGNORECASE)
_TAGGED_AGE = re.compile(r"\bage[ds]?\s*:?\s+(\d{1,2})(?:\s*[-–—/]\s*(\d{1,2}))?\b", re.IGNORECASE)
_UK_BAND = re.compile(r"\b(?:uk|gb)\s*(?:size\s*)?(?:2|4|6|8|10|12|14|16|18|20|22|24|26|28|30)\b", re.IGNORECASE)
_CHILD = re.compile(r"\b(?:kids?(?:wear)?|child(?:ren)?s?(?:wear)?|juniors?|youths?|teens?(?:wear)?|teenage(?:rs?)?|"
                    r"toddlers?|infants?|newborns?|babies|girls?(?:wear)?|boys?(?:wear)?)\b", re.IGNORECASE)
_ADULT = re.compile(r"\b(?:adults?|womens?(?:wear)?|woman|ladies|mens?(?:wear)?|man)\b", re.IGNORECASE)
_FEMALE = re.compile(r"\b(?:womens?(?:wear)?|woman|ladies|female|girls?(?:wear)?)\b", re.IGNORECASE)
_MALE = re.compile(r"\b(?:mens?(?:wear)?|man|male|boys?(?:wear)?)\b", re.IGNORECASE)
_EXPLICIT_WEARER = re.compile(
    r"\b(?:kids?|child(?:ren)?|juniors?|teens?|youth|girls?|boys?|women(?:s)?|men(?:s)?|ladies)"
    r"(?:['’]s?)?\s+(?:size|aged?)\b", re.IGNORECASE)


def _public_text(value):
    if isinstance(value, (list, tuple)):
        return " ".join(_public_text(v) for v in value[:12])
    if isinstance(value, dict):
        value = value.get("name") or value.get("title") or value.get("label")
    return value[:320].casefold() if isinstance(value, str) else ""


def _age_band(match, *, months=False):
    lo, hi = int(match[1]), int(match[2] or match[1])
    if not (0 <= lo <= hi <= (36 if months else 18)) or (not months and lo >= 18):
        return None
    return (lo, hi) if months else (lo*12, hi*12+11)


def _age_number(value):
    return type(value) is int or (isinstance(value, str) and 0 < len(value) < 4 and value.isascii() and value.isdigit())


def audience_fields(raw):
    """Keep only publicly stated audience labels, size evidence, and age bands in months."""
    extra = raw.get("audience")
    extra = extra if isinstance(extra, dict) else {}
    title = _public_text(raw.get("title"))
    size = _public_text(raw.get("size") or raw.get("size_title"))
    departments = " ".join(_public_text(raw.get(k)) for k in
                           ("department", "gender", "age_group", "category", "leaf_title", "category_path",
                            "audienceType", "suggestedGender"))
    if isinstance(raw.get("audience"), str):
        departments += " " + _public_text(raw["audience"])
    departments += " " + " ".join(_public_text(extra.get(k)) for k in ("audienceType", "suggestedGender", "gender"))
    # Baby tees, boyfriend jeans and kidskin are styles/materials. Adult size XS alone is age-neutral.
    text = " ".join((title, size, departments, _public_text(raw.get("brand"))))
    text = re.sub(r"\b(?:baby (?:tees?|t[ -]?shirts?|dolls?|blue|pink)|girls?['’]? night(?: out)?|"
                  r"boys?['’]? lie|girls? aloud|teenage mutant ninja turtles)\b", " ", text)
    desc = _public_text(raw.get("description"))
    text += " " + " ".join(m[0] for m in _EXPLICIT_WEARER.finditer(desc))
    ages, genders, bands = set(), set(), set()
    for key, valid, store in (("audience_age", ("adult", "child", "conflict"), ages),
                              ("audience_gender", ("male", "female", "unisex", "conflict"), genders)):
        if raw.get(key) in valid:
            store.add(raw[key])
    prior = raw.get("audience_age_band")
    if isinstance(prior, str) and re.fullmatch(r"\d{1,3}-\d{1,3}", prior):
        bands.add(tuple(map(int, prior.split("-"))))
    if prior == "conflict":
        ages.add("conflict")
    # A compact size such as 10Y is an age. A bare S, XXS or 8–10 is not.
    age_input = f"{title} {size} {departments}"
    # Only a stated size/age or a wearer age in the description is personal sizing evidence.
    for mention in _AGE.finditer(desc):
        if re.match(r"(?:size|age)", mention[0]) or mention[3].lower().endswith("old"):
            age_input += " " + mention[0]
    for match in _EXPLICIT_WEARER.finditer(desc):
        age_input += " " + desc[match.start():match.end()+17]
    for pattern, value in ((_AGE, age_input), (_TAGGED_AGE, _AGE.sub(" ", age_input))):
        for match in pattern.finditer(value):
            months = pattern is _AGE and match[3].lower().startswith("m")
            band = _age_band(match, months=months)
            if band:
                bands.add(band)
    minimum = extra.get("suggestedMinAge", raw.get("suggestedMinAge"))
    maximum = extra.get("suggestedMaxAge", raw.get("suggestedMaxAge"))
    if _age_number(minimum) and _age_number(maximum) and 0 <= int(minimum) <= int(maximum) <= 18 and int(minimum) < 18:
        bands.add((int(minimum)*12, int(maximum)*12+11))
    if _age_number(minimum) and int(minimum) >= 18:
        ages.add("adult")
    if _age_number(maximum) and 0 < int(maximum) < 18:
        ages.add("child")
    toddler_size = re.fullmatch(r"(?:size\s+)?[1-6]\s*t", size.strip())
    baby_item = re.search(r"\bbaby(?:['’]s)?\s+(?:jacket|coat|romper|sleepsuit|onesie|clothes)\b", title)
    if bands or toddler_size or baby_item or _CHILD.search(text) or re.search(r"\bbaby\b", departments):
        ages.add("child")
    if _ADULT.search(text):
        ages.add("adult")
    # Category/age text attached to a children's shoe size does not turn it into adult clothing.
    if (garment(title) or garment(departments)) and _UK_BAND.search(size) and not _AGE.search(size) and not _TAGGED_AGE.search(size):
        ages.add("adult")
    if _FEMALE.search(text):
        genders.add("female")
    if _MALE.search(text):
        genders.add("male")
    if re.search(r"\bunisex\b", text):
        genders.add("unisex")
    result = {}
    if bands:
        lo, hi = max(b[0] for b in bands), min(b[1] for b in bands)
        result["audience_age_band"] = f"{lo}-{hi}" if lo <= hi else "conflict"
        if hi < lo:
            ages.add("conflict")
    if ages:
        result["audience_age"] = ages.pop() if len(ages) == 1 else "conflict"
    if genders:
        result["audience_gender"] = ("conflict" if "conflict" in genders else "unisex" if "unisex" in genders
                                     else genders.pop() if len(genders) == 1 else "conflict")
    return result


def _audience_mismatch(a, b):
    x, y = audience_fields(a), audience_fields(b)
    if "conflict" in (*x.values(), *y.values()):
        return "Conflicting age, size or department"
    ages = {x.get("audience_age"), y.get("audience_age")}
    if "child" in ages and len(ages) > 1:
        return "Different or unconfirmed children's department"
    if {x.get("audience_gender"), y.get("audience_gender")} == {"male", "female"}:
        return "Different gender department"
    bands = x.get("audience_age_band"), y.get("audience_age_band")
    if all(bands) and bands[0] != bands[1]:
        return "Different children's age sizes"
    return None


def _same_known_family(target, other, kind_a, kind_b):
    if not kind_a or not kind_b:
        return False
    if kind_a == kind_b:
        return True
    if {kind_a, kind_b} <= {"jacket", "coat"}:
        leaf = target.get("vinted_catalog_id")
        if leaf and leaf == other.get("vinted_catalog_id"):
            return True
        return bool(_features(target)["outer construction"] & _features(other)["outer construction"])
    return False


def same_garment_family(target, other):
    kind_a, conflict_a = family_facts(target)
    kind_b, conflict_b = family_facts(other)
    return (not conflict_a and not conflict_b and _same_known_family(target, other, kind_a, kind_b)
            and not _audience_mismatch(target, other))


def family_mismatch(target, other):
    audience = _audience_mismatch(target, other)
    if audience:
        return audience
    a, conflict_a = family_facts(target)
    b, conflict_b = family_facts(other)
    if conflict_a or conflict_b:
        return "Conflicting listing title, category or photo"
    if (a or b) and not _same_known_family(target, other, a, b):
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
    wear_note = wear_difference(target, other)
    if wear_note:
        return "context", wear_note
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
    if a != b:
        left, right = ", ".join(sorted(a - b)[:3]), ", ".join(sorted(b - a)[:3])
        tier = "close" if strong_style_match(target, other) else "context"
        return tier, reason + (("Unconfirmed on comparison: " + left + ". ") if left else "") + (
            ("Unconfirmed on target: " + right + ".") if right else "")
    return "close", reason + "Same stated condition."
