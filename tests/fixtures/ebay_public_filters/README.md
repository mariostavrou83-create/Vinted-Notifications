These small fixtures preserve checked filter controls and applied chips from
anonymous eBay UK responses captured on 11 October 2026. Full scripts, cookies,
tracking URLs and listing bodies have been removed. The `0 results` heading
and sort container are synthetic scaffolding for filter-contract tests; they
do not assert the real captured searches had zero inventory.

`7fam_ignored.html` was returned for the API-facing single-encoded multiword
brand. `7fam_native.html` and `multi_size_native.html` were returned after native
double encoding. The auction and fixed-price controls both use `value=1`;
their associated `aspectURLParam` identifies the actual selected listing type.
