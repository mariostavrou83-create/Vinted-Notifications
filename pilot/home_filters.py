"""Fixed UK catalogue choices used by both the Home form and the alert worker."""
import re

from .inputs import InputError

# UK marketplace departments. Designer is a collection and needs explicit listing evidence.
DEPARTMENTS = ("Women", "Men", "Kids", "Home", "Electronics", "Books & Media",
               "Hobbies & collectables", "Sports", "Designer")
GARMENTS = ("Clothing", "Shoes", "Bags", "Accessories")
WOMEN = (*GARMENTS, "Dresses", "Tops & T-shirts", "Skirts", "Jeans", "Trousers", "Coats & jackets", "Jumpers & sweaters")
MEN = (*GARMENTS, "Tops & T-shirts", "Shirts", "Jeans", "Trousers", "Coats & jackets", "Jumpers & sweaters")
CATEGORIES = {
    "Departments": DEPARTMENTS,
    "Women": tuple("Women / " + name for name in WOMEN),
    "Men": tuple("Men / " + name for name in MEN),
    "Kids": ("Kids / Clothing", "Kids / Shoes"),
}
LETTERS = ("XXS", "XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL", "5XL", "6XL", "7XL", "8XL")
SIZES = {
    "Clothing": LETTERS,
    "UK clothing": tuple(f"UK {n}" for n in range(2, 35, 2)),
    "Waist (inches)": tuple(f"W{n}" for n in range(24, 51)),
    "UK shoes": tuple(f"UK shoe {n/2:g}" for n in range(2, 33)),
    "Children": ("Newborn", "Up to 1 month", "1–3 months", "3–6 months", "6–9 months",
                 "9–12 months", "12–18 months", "18–24 months", "24–36 months", "2 years", *(f"{n} years" for n in range(3, 17))),
    "Other": ("One size",),
}
NON_CLOTHING = {"Home", "Electronics", "Books & Media", "Hobbies & collectables"}
ALIASES = {"women's":"women","womens":"women","women's clothing":"women / clothing",
           "men's":"men","mens":"men","men's clothing":"men / clothing","children":"kids",
           "entertainment":"books & media", "books and media":"books & media",
           "hobbies & collectibles":"hobbies & collectables","tops & t-shirts":"tops & t-shirts",
           "tops and t-shirts":"tops & t-shirts","coats and jackets":"coats & jackets",
           "jumpers and sweaters":"jumpers & sweaters","one-size":"one size","one size fits all":"one size",
           "xxxl":"3xl","xxxxl":"4xl","2xl":"xxl","newborns":"newborn"}
CAT_IDS = {1904:"Women", 5:"Men", 1193:"Kids", 10:"Women / Dresses"}
TIMES = tuple(f"{hour:02}:{minute:02}" for hour in range(24) for minute in (0, 15, 30, 45))


def norm(value):
    text = str(value or "").replace("’", "'").replace("–", "-").replace("—", "-").casefold()
    text = " ".join(text.split())
    return ALIASES.get(text, text)


def size_groups(category):
    if category in NON_CLOTHING:
        return ()
    department, _, kind = category.partition(" / ")
    if kind == "Shoes":
        return ("UK shoes",)
    if kind in ("Bags", "Accessories"):
        return ("Other",)
    if department == "Kids":
        return ("Children", "Other") if kind else ("Children", "UK shoes", "Other")
    if department in ("Women", "Men"):
        groups = ["Clothing", "UK clothing", "Other"]
        if kind in ("", "Clothing", "Jeans", "Trousers", "Skirts"):
            groups.append("Waist (inches)")
        if not kind:
            groups.append("UK shoes")
        return groups
    return list(SIZES)


def options(settings):
    result = {}
    for field, groups in (("categories", CATEGORIES), ("sizes", SIZES)):
        choices = {norm(v): v for group in groups.values() for v in group}
        selected = [choices.get(norm(v), v) for v in settings.get(field, [])]
        legacy = [v for v in selected if norm(v) not in choices]
        result[field] = {"groups": dict(groups), "selected": selected}
        if legacy:
            result[field]["groups"]["Previously saved"] = legacy
    result["size_groups"] = {category: size_groups(category) for group in CATEGORIES.values() for category in group}
    result["times"] = sorted(set(TIMES) | {t for t in (settings.get("quiet_start"), settings.get("quiet_end")) if t})
    return result


def validate(current, saved, data):
    choices = options(current)
    for field in ("categories", "sizes"):
        if field in data:
            valid = {norm(v): v for group in choices[field]["groups"].values() for v in group}
            try:
                saved[field] = list(dict.fromkeys(valid[norm(v)] for v in saved[field]))
            except KeyError:
                raise InputError(f"Choose {field} from the dropdown.") from None
    for field in ("quiet_start", "quiet_end"):
        if field in data and saved[field] and saved[field] not in choices["times"]:
            raise InputError("Choose a quiet-hour time from the dropdown.")


def category_terms(item):
    path = item.get("category_path")
    parts = list(path) if isinstance(path, list) else [path]
    parts.append(item.get("category"))
    attrs = item.get("attributes")
    ids = attrs.get("category_ids", []) if isinstance(attrs, dict) else []
    if isinstance(ids, list):
        parts.extend(CAT_IDS[n] for n in ids if type(n) is int and n in CAT_IDS)
    if type(item.get("category_id")) is int:
        parts.append(CAT_IDS.get(item["category_id"]))
    terms = {term for part in parts if isinstance(part, str)
             for term in re.split(r"\s*(?:>|/|›)\s*", norm(part)) if term}
    terms = {fragment for term in terms for fragment in norm(term).split(" / ")}
    if terms & {"coats", "jackets", "biker jackets", "blazers", "bomber jackets"}:
        terms.add("coats & jackets")
    if terms & {"jumpers", "sweaters", "cardigans"}:
        terms.add("jumpers & sweaters")
    if terms & {"tops", "t-shirts"}:
        terms.add("tops & t-shirts")
    if terms & {"dresses","tops & t-shirts","shirts","skirts","jeans","trousers","coats & jackets","jumpers & sweaters"}:
        terms.add("clothing")
    if item.get("designer") is True:
        terms.add("designer")
    return terms


def size_terms(value, shoe):
    label = norm(value)
    terms = {label}
    for part in re.split(r"\s*[/|]\s*", label):
        terms.add(norm(part))
        numeric = re.fullmatch(r"(?:uk\s*)?(\d{1,2}(?:\.5)?)(?:\s*-\s*(\d{1,2}))?", part)
        if numeric:
            lo, hi = numeric.groups()
            if shoe:
                terms.add("uk shoe " + lo)
            elif hi and lo.isdigit():
                terms.update(f"uk {n}" for n in range(int(lo), min(int(hi), 40) + 1, 2))
            elif not hi:
                terms.add("uk " + lo)
        waist = re.match(r"w\s*(\d{2})(?:\b|l)", part)
        if waist:
            terms.add("w" + waist[1])
    return terms


def matches(settings, item):
    selected = settings.get("categories") or []
    terms = category_terms(item)
    def includes(category):
        return all(norm(piece) in terms for piece in category.split(" / "))
    if selected and not any(includes(c) for c in selected):
        return False
    sizes = settings.get("sizes") or []
    if not sizes or (selected and any(c in NON_CLOTHING and includes(c) for c in selected)):
        return True
    actual = size_terms(item.get("size"), "shoes" in terms)
    return any(norm(s) in actual for s in sizes)
