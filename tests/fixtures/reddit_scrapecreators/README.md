# ScrapeCreators Reddit response fixtures

Selected fields from the GET response examples at
https://docs.scrapecreators.com/openapi.json, retrieved September 15, 2026.
Titles, authors and body text are replaced with neutral placeholders. Provider
ids, response structures, nulls, numeric values and cursors are preserved.
These are documentation fixtures, not proof of live API success or completeness.

The comment example contains obsolete comma-separated cursors. The endpoint's
current documentation says to pass one opaque cursor unchanged, never comma
batches. Tests deliberately verify that Kern reports this gap rather than
inventing pagination or claiming it fetched the complete thread.
