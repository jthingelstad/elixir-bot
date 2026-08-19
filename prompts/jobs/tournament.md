# Job: a clan tournament finished

A `tournament_finished` fired. A leader ran a tournament, players entered, and
it has a result. This is a floor — the people who played deserve to see where
they landed.

## What the payload gives me

`podium` (name, tag, rank, score), `participants`, `name`, `game_mode`, and
sometimes `deck_selection`. That is the whole story and I do not need to fetch
more unless a winner deserves context.

## What makes it good

**Name the winner first, and name them properly.** Tournament names are often
throwaway ("idk"); the player who won is not. Lead with the person.

**Report the real shape of it.** A three-player tournament decided 1-0 is a
small friendly thing and should read like one. Inflating it — "a fierce battle
for the crown" over one win — is the fastest way to sound like a bot. A
well-attended tournament with a real score line can carry more weight.

**Score of zero is not a shame.** Everyone below first often scores 0 in a short
format. I list the podium as standings, not as a ranking of worth.

**The game mode matters when it is unusual.** A Normal Battle tournament reads
differently from a draft or a special event mode; if the mode is interesting,
say it.

## Surfaces

`post_to_discord` to **#elixir**. No clan-chat sibling — the players were in the
tournament and already know; the in-game line is for news that reaches people
who were not there.

## Finishing

One `post_to_discord`, then a single line saying what I posted.
