# Fresh Home discovery

The Connections page controls the Home monitor for the original and buying
accounts. It uses their ordinary authenticated Home views. Each available
account targets one feed scan per minute and up to six item pages per cycle.
A cooldown on one account leaves the other eligible. The existing scheduler,
account locks and checkout priority apply.

The item page must identify the same listing, show availability and a GBP price,
and give a verified upper bound for the upload age. Unknown ages and rounded
three-minute labels are excluded. Cache age, elapsed processing time and the
queue are counted. Delivery checks the three-minute limit again before submitting
to Telegram. The shared item claim prevents duplicates across Home and saved
searches. Home alerts retain the exact saved query and its price cap.

The matcher respects active queries, budgets, categories, sizes, exclusions and
must-have terms. It supports literal terms, equivalent spellings, and individual
garment features from the owner's open-label styles. Scope can use the target
page's verified women's breadcrumb and an unambiguous displayed garment size.
An eligible, fresh listing with incomplete style wording can use one cover photo
for utility jackets, leather-look jackets, lace camis or shaped skirts. Both
a visual-description test and proximity to scraper examples are required. This
is visual similarity, not verification of fabric composition. The other style
families use seller-described features. Photos do not establish age, price,
brand, size, availability or condition. Photo suggestions are labelled in the
dashboard and Telegram.

A short-lived CPU subprocess loads the quantized OpenAI CLIP ViT-B/32 image
encoder only for otherwise-unmatched, eligible Home covers. One capped download
and one inference serve all eligible profiles. The model is verified and
installed in the image at build time. The reference file contains quantized
vectors, without the workbook, seller IDs or original photographs. The container
includes the model's MIT license in style_assets/CLIP-LICENSE.txt.
Source: https://huggingface.co/Xenova/clip-vit-base-patch32/tree/6b7c523703ae26e39be4b03774966f8571c7f523

The Connections page reports sent and pending Home alerts in the last 24 hours,
and per-account counts for freshness, scope, budget, style, image checks and
matches. The account cards show the most recent scan cycle. Recent Finds identifies
Home results and refreshes while the newest page is visible.
