# Instagram Ads readback

`engagement_readback.json` contains the safe campaign, ad-set and nested ad/creative configuration from a live Meta Marketing API v25.0 existing-Reel engagement launch on 2026-10-10. Kern stopped during paused verification, before activation. Numeric resource IDs and the campaign label are anonymized. The original public caption and actual returned fields, including omissions, basic timezone offsets, `POST_INTERACTION` and generated creative label, are retained. No tokens, URLs with credentials or billing identifiers are included.

The tests replay this shape through both verification barriers, using the same source caption and approved UTC flight, and separately reject changes to delivery, identity, creative and destinations. The fixture establishes provider serialization and resource creation, not activation, delivery or spend.
