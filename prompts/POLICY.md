# POAP KINGS Policies

Eligibility, cap, and demotion rules for clan leadership. The rules below are documented for human leaders to read. Enforcement happens at the tool layer (`get_promotion_candidates`, `evaluate_elder_eligibility`); the LLM should trust the `recommended`, `demotion_candidates`, and `elder_cap_reached` fields rather than re-deriving these rules.

## Promotions

Elder is earned, not automatic.

- The primary path to Elder is consistent card donations relative to the clan.
- There is no fixed donation-count floor. If the clan donates more, Elder means donating more.
- Active battle play is required. Logging in without battling is not activity.
- Recent war participation is required for Elder.
- The tool ranks current Members and Elders by a smoothed rolling average of weekly donation peaks, sorted descending.
- Promotions should preserve role balance and meaning by honoring the Elder cap.

`get_promotion_candidates` returns the current smoothed donation leaderboard, promotion recommendations for non-Elders inside the capped Elder set, and demotion recommendations for current Elders outside it. Trust the tool's filtering instead of inventing an absolute donation bar.

## Clan Composition

- For every 10 members: about 1 leader or co-leader, 2-3 elders, and the rest members.
- Hard cap: no more than 3 elders per 10 active members.
- Not everyone should be Elder. The role should retain meaning and trust.

The cap is enforced by `get_promotion_candidates`. When the current Elder set and the capped donation leaderboard disagree, recommend role changes that move the Elder group toward the top ranked active war participants.

## Demotion and Removal

- Elder demotion risk is based on the same smoothed leaderboard as promotion. A current Elder should be considered for demotion when they fall outside the capped Elder set or fail the battle/war activity gates.
- Removal is primarily about inactivity and absence.
- For removal-candidate flagging, inactivity is trophy-scaled (computed in `storage/war_analytics.py`; surfaced via at-risk tooling).
- When the clan has open slots and is still building its bench, leaders can be more flexible with removal decisions. Once the roster is full, removal calls tighten up.
- The `get_promotion_candidates` tool returns `demotion_candidates` alongside promotion recommendations — review both together.
- Discuss promotions, demotions, and kicks only in private clan leadership channels.
