# POAP KINGS Policies

Eligibility, cap, and demotion rules for clan leadership. The numerical thresholds below are documented for human leaders to read. Enforcement happens at the tool layer (`get_promotion_candidates`, `evaluate_elder_eligibility`); the LLM should trust the `recommended` list and the `elder_cap_reached` flag rather than re-deriving these rules.

## Promotions

Elder is earned, not automatic.

- The primary path to Elder is consistent card donations and being a good clan member.
- Reliability and contribution matter more than one flashy week.
- If several members are strong donators, war participation can be used as a tiebreaker.
- Tenure matters, but behavior matters more.
- Promotions should preserve role balance and meaning.

Tenure floor (21 days) and donation floor are enforced by `get_promotion_candidates`. Members below either floor will not appear in the `recommended` list. Trust the tool's filtering instead of re-checking thresholds.

## Clan Composition

- For every 10 members: about 1 leader or co-leader, 2-3 elders, and the rest members.
- Hard cap: no more than 3 elders per 10 active members.
- Not everyone should be Elder. The role should retain meaning and trust.

The cap is enforced by `get_promotion_candidates` via the `elder_cap_reached` flag. When that flag is true, focus on demotion review and weaker-elder evaluation instead of new promotions.

## Demotion and Removal

- Elder demotion risk is primarily tied to donations stopping. The pattern matters more than a single off week — if an Elder's donations drop below the meaningful threshold for two consecutive weeks, recommend demotion.
- Removal is primarily about inactivity and absence.
- For removal-candidate flagging, inactivity is trophy-scaled (computed in `storage/war_analytics.py`; surfaced via at-risk tooling).
- When the clan has open slots and is still building its bench, leaders can be more flexible with removal decisions. Once the roster is full, removal calls tighten up.
- The `get_promotion_candidates` tool returns `demotion_candidates` alongside promotion recommendations — review both together.
- Discuss promotions, demotions, and kicks only in private clan leadership channels.
