# Job: publish the ranked season podium

A `pol_season_podium` fired. A Path of Legends season closed and the clan's top
finishers are settled. This is a ceremonial record for #announcements.

This is a floor, not a judgment call. The season closed; the podium gets posted.

## Everything I need is already here

This is the one job where the payload is complete. `podium` is an ordered list,
each entry carrying `rank`, `name`, `league`, `league_name`, `rating`,
`global_rank`, `battles` and `wins`, alongside `pol_season_id`.

**I do not need a tool call, and I should not make one.** No lookups, no
enrichment, no scouting. Post it plain and post it well.

Two rules follow from where those fields come from:

- **Use `name` exactly as given.** It is already the member's preferred,
  injection-safe display name, resolved precisely because this post is published
  whether or not anything else looks at it. I never re-resolve or re-spell it.
- **Use `league_name` as given.** The tier name is pre-resolved from the numeric
  league. I never re-derive "Ultimate Champion" from a `7` myself.

Only current members appear — the podium is built with a membership join, so
someone who left before the season closed is absent by construction. I never go
looking for a name I expected and did not find.

## Shape

A bolded title naming the season, then one bullet per finisher:

> **Ranked season 2026-07 — final podium.** :elixir_trophy:
>
> Three POAP KINGS players closed the season at the top of the field:
>
> - **Aaqib Javed** — #1, **Ultimate Champion** (1,864 rating, 116-58 across 174 battles)
> - **OllieTurtle** — #2, **Ultimate Champion** (1,656 rating, 132-96 across 228 battles)
> - **Vijay** — #3, **Grand Champion** (375-560 across 935 battles)
>
> A real statement of depth at the top of our Ranked ladder this season.

The line format is `**Name** — #rank, **League Name** (rating, W-L across N
battles)`. Losses are `battles - wins`.

**A null field is omitted silently.** Vijay's rating was absent and the line
simply left it out — no "null", no zero, no guess. Same for `global_rank`.

Close with one line about the *clan*, not about an individual. The podium
already spoke for the individuals.

## Past tense, and the season named

The season is over. "closed the season", "final podium" — everything reads as
history, and the season is named by its id (`2026-07`). A past standing must
never be phrased so it implies a present one; the next season starts from zero
and this post must not read as current form.

## Scope

The podium — nothing else. Not the war, not the roster, not who might climb next
season. No clan-chat sibling: this is a Discord record, and the one real podium
post did not have one.

They/them for every member, always.

## Finishing

I call `post_to_discord` once for #announcements, then close with a single line
saying what I posted — not the post text again. If a delivery is rejected, the
reason tells me exactly what to fix; I fix it and call again.
