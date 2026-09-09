# Facts from the real landing zone (85,775 frames, Argonauts, 2026-09-09 10:20–11:00 UTC)

Measured by the orchestrator on the operator's machine. Inputs to the design / quant / data-engineering proposals. Not opinions.

## Event mix in 40 minutes
item_received_bid 42,601 · item_cancelled 41,361 · order_invalidate 1,529 · collection_offer 168 · trait_offer 39 · item_listed 8 · item_transferred 3 · item_sold 3 · order_revalidate 1.

## Trait offers CARRY THEIR CRITERIA
`trait_offer` payload fields: `trait_criteria: {trait_type, trait_name}` (single) and `trait_criteria_list: [{trait_type, trait_name}, ...]` (multi — an AND across traits), plus `numeric_trait_criteria_list`. Examples seen: `Print: Unclaimed` ×10, `Palette: Seafoam` ×3, `[Cloak: Ivory AND Print: Unclaimed]`, `[Bones: Petrified AND Print: Unclaimed]`. 16 of 39 had only the list form (single field null).
→ Trait offers can be matched to a trait filter exactly. The current "exclude trait offers under a filter" rule (BUG-045) can be replaced by matching on stored criteria. The store does not yet keep these fields.

## Stream item payloads do NOT carry traits or metadata_url
`item.metadata` = {name, image_url (on i2c.seadn.io — an OpenSea host), metadata_url: null, traits: null}. So the stream alone cannot supply traits. It does supply the token id and name of every token that trades or is bid on: 3,170 distinct token ids seen in 40 minutes (ids 4..9999).

## Token ids
Contract 0x387c41b0b2f1128de44db1bcf8baad085f26392c. Ids observed 4..9999 — suggests a numeric range, not necessarily contiguous. Collection size per OpenSea: 9,212 (from earlier session).

## Budget facts
REST: 120 reads/hour, measured -- the older figure in early docs was unsourced and wrong (BUG-003). Token list = ~47 reads (200 per page). Per-token endpoint = 1 read each. Stream = unmetered.
