"""Home-only styles from the owner's buying workbook, with independent all-in limits."""
from contextlib import closing
from typing import NamedTuple
from urllib.parse import urlencode

import search_settings
from logger import get_logger

logger = get_logger(__name__)


class Guide(NamedTuple):
    key: str
    name: str
    category: str
    ceiling: int
    example_search: int
    example_name: str
    advice: str


GUIDES = (
    Guide("layered_henley", "Fitted or layered Henley", "12", 900, 158, "henley",
          "Look for an attached vest, mock layer or fitted button placket with waffle knit, ribbing or contrast trim. Skip plain basics and worn necklines."),
    Guide("fur_hood_bomber", "Fitted fur-hood jacket or bomber", "1037", 1400, 184, "fur-hood",
          "Look for a full, even hood trim and a defined waist, short hem or ribbed waistband. Check the zip, cuffs and hood attachment."),
    Guide("beaded_evening_top", "Beaded evening cami or halter", "12", 1000, 153, "beaded",
          "Look for beading or embroidery on silk, satin, chiffon or a shaped evening cut. Check the straps and for missing beads or pulls."),
    Guide("foldover_flares", "Fold-over waistband flares", "9", 900, 266, "yoga",
          "Look for a visibly folding waistband and a proper flared or bootcut leg. Check the waistband recovery, knees and heel wear."),
)


def migrate(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS vinted_home_guides (
        query_id INTEGER PRIMARY KEY REFERENCES queries(id) ON DELETE CASCADE,
        style_key TEXT NOT NULL UNIQUE)""")


def install():
    """Add guides once when the owner's original comparison pictures are available."""
    added = []
    with closing(search_settings.connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for guide in GUIDES:
            marker = "home_guide_installed_" + guide.key
            if conn.execute("SELECT 1 FROM parameters WHERE key=?", (marker,)).fetchone():
                continue
            # Use original clothing pictures. Brand-specific pricing printed on
            # an annotated board does not transfer to an open-label Home find.
            source = conn.execute("""SELECT d.reference_id FROM queries q
                JOIN search_dashboard d ON d.query_id=q.id
                JOIN dashboard_media m ON m.id=d.reference_id
                WHERE q.id=? AND instr(lower(q.query_name),?)>0""",
                (guide.example_search, guide.example_name)).fetchone()
            if not source:
                continue
            name = "Home only · " + guide.name
            url = "https://www.vinted.co.uk/catalog?" + urlencode({
                "catalog[]": guide.category, "size_ids[]": "2,3,4", "search_text": guide.name})
            qid = conn.execute("INSERT INTO queries(query,query_name) VALUES (?,?)", (url, name)).lastrowid
            conn.execute("INSERT INTO vinted_home_guides VALUES (?,?)", (qid, guide.key))
            conn.execute("INSERT INTO search_dashboard(query_id,reference_id) VALUES (?,?)", (qid, source[0]))
            conn.execute("""INSERT INTO search_reference_photos(query_id,position,media_id)
                SELECT ?,position,media_id FROM search_reference_photos WHERE query_id=?""",
                (qid, guide.example_search))
            conn.execute("INSERT INTO vinted_search_budgets(query_id,max_total) VALUES (?,?)", (qid, guide.ceiling))
            conn.execute("INSERT INTO search_platforms(query_id,vinted_enabled,ebay_enabled) VALUES (?,1,0)", (qid,))
            advice = f"Up to £{guide.ceiling // 100} including fees and postage. {guide.advice} Photos show the cut across labels."
            conn.execute("INSERT INTO finder_buy_guides VALUES (?,?,?,?,?)", (qid, source[0], advice, name, None))
            conn.execute("INSERT INTO parameters(key,value) VALUES (?,?)", (marker, str(qid)))
            added.append((qid, guide.key, guide.ceiling))
    if added:
        logger.info("Created Home-only guides (ID, style, maximum pence): %s", added)
    return len(added)
