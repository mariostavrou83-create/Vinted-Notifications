# Audited search release — 20 October 2026

The owner's 128 shared searches replace overly broad discovery and restore
useful old coverage. Keep the 40 retained old Vinted searches live for a
comparison period. Juicy, 7 For All Mankind and archived/test searches stay out
of the active cohorts. Do not add True Religion or Adidas capri replacements.

The release preserves current new all-in maximums, resale targets, folders,
ordered reference originals and purchase settings. Restore Vinted size IDs
2/3/4 (XS/S/M) and corresponding saved eBay size aspects. Configured eBay links
remain saved; the live eBay selection is empty. Rare-brand discovery may be
broader. Monsoon silk dresses, Dorothy Perkins outerwear and Superdry parkas
use narrower style/model alternatives. Restore Bench snood/thumbhole and generic
coat wording, STQ/Custo top coverage and Diesel graphics/long sleeves.

Exclusions concern unwanted items in the same category. Fleece lining is
allowed in the wanted outerwear. Ordinary outer-fleece, teddy/borg and aviator
styles are excluded where relevant. Wanted fitted Bench signature fleece is
retained through its own positive terms without a blanket fleece exclusion.
Fitted all-over faux fur and hoodless lined/padded gilets can qualify; compare
their own style/value rather than inheriting the highest reference target.
Early 2010s items are acceptable. Neck labels support identification, and do not
form an age whitelist; style, condition and clear recent-release evidence matter.
No image inference is added to the notification path: a one-second publication
to Telegram guarantee has not been established, and no additional inference
latency is authorised.

## One notification, multiple match records

The owner's final preference is one notification per actual Vinted listing.
Keep global `items`/`alert_outbox` and buyer identity unchanged. An additive
`search_match_log` records every observed qualifying search and its old/new
cohort, revision, rule snapshot, keyword evidence, estimated budget and match
time. The original alert row supplies the notified search, send status and
timestamps. This includes later description-proven matches. A listing with
both cohorts has one notification and a logged overlap. Multiple matches
within either cohort are logged too. A lower new all-in cap can legitimately
produce an old-only match.

This is observed marketplace coverage, not proof that Vinted returned all
possible listings. Quiet first-page baselines, exact URL variants and catalogue
checkpoints remain in force. Existing history does not become new matches.
Download the owner-authenticated JSON log from the dashboard comparison notice.

## Budget correction and rollout

Shared Vinted alerts now respect valid supplied protection-inclusive prices.
If unavailable, use the explicit fallback of rounded 5% plus £0.70, then £2.20
estimated postage. A £7 item with a supplied protected price of £8.05 therefore
plans at £10.25, not £9.55. Actual checkout still checks the actual full total
against its saved limits. Preserve the supplied price through deferred keyword
proof as well as initial discovery and delivery revalidation.

Schema 26 adds only audit tables and their indexes. The one-time settings
release requires `MSJ_SEARCH_AUDIT_RELEASE=msj-search-audit-20261020-v1`.
The definitions are a bounded, checksum-verified private deployment payload;
they are not committed to this public repository. Automated tests use synthetic
owner data, with separate local replay against the actual approved payload.
Before applying, create and integrity-check a private SQLite backup, compare
all 128 expected definitions/photo counts and the exact 40 active old IDs,
validate every resulting filter and apply everything in one transaction. Any
owner edit aborts the entire settings release. Failure retains prior saved
settings and monitoring. An applied marker prevents repeat edits on restart.

New size-filter URLs start with quiet variant baselines. No past-stock replay,
synthetic Telegram alert, live checkout, payment or buyer-setting mutation is
part of this deployment. Offline tests cover full release preservation,
rollback/idempotence, category noise/positive recall, exact budget boundaries,
concurrent old/new matches, description proof, historical/baseline suppression
and authenticated audit export.
