# Points That Travel

This is the companion to [my post on engagement farming](https://coderlegion.com/29904/i-am-jacks-profile-engagement). That post is the idea. This repo is the machinery: the full system, the simulation, every way we tried to break it, and the places where it still breaks.

The one-line version of the whole thing:

> **Make farming cost what real participation costs.**

## The problem, briefly

Free engagement carries almost no information once bots exist. A like costs nothing, so a farm can produce endless likes, and every one of them looks the same as a like from a real reader. Adding a second free reaction (a "love" next to the "like") doesn't help, because it just gives the farm a second button to press.

So this design treats engagement as an economy. Every like spends points the member earned. What matters is how those points *move* through the community: how fast and, more importantly, how far.

## What the simulation found

We built an agent-based model with 593 accounts, ran it for 35 days (the first 10 as warm-up), and averaged everything over 5 random seeds. The variation between seeds was under half a percentage point everywhere.

The population:

| group | accounts | what they do |
|---|---|---|
| novel posters | 60 | real members who write things people want to read |
| regular members | 200 | real members, average quality |
| middle users | 200 | real, attentive members who don't add much that's new |
| pod | 12 | real people who agreed to engage with each other's posts |
| farm ring | 40 | farm accounts liking each other, holding high points from the *old* system |
| fake-account swarm | 80 + 1 | brand new accounts all boosting one beneficiary |

Farm accounts are 20.4% of the population.

The headline:

| | farm share |
|---|---|
| free likes and loves (today's model) | **63.6%** of all engagement |
| this design, everything on | **2.5%** of the points paid out |
| …of which the farm generated itself | **0.0%** |

Under this design, everything the farms still receive comes from real readers liking their posts. Their own engagement with each other produces nothing.

The simulation also changed the design in four places:

1. **Trust on its own made things worse.** When a careful farm beat the reading-time check, the trust score (a PageRank variant, explained below) looped around the ring indefinitely. The ring ended up holding **70% of all trust with 6.7% of the accounts**. That's the closed loop the whole design is supposed to punish.
2. **Measuring how far points travel fixed it.** Weighting each member by how far their spending actually reaches cut the careful farm's self-generated payout from **84.6% to 0.5%**. That measure is now part of the formula as φ (phi).
3. **The daily allowance is required, and it's safe.** Without it, the middle users stop engaging, which removes the jury that decides what's novel. Fake accounts get the allowance too, and it buys them nothing.
4. **Flags carry the last stretch, and they depend on the moderators.** Filler that real readers genuinely like gets past every structural safeguard. Flags with a cost cut it from **8.9% to 2.5%** when moderators are right 90% of the time, and to **3.5%** at 60%.

## The system, one day at a time

Everything runs in daily rounds. Each round is computed from the state the previous round left, so the circular parts (trust depends on spending, spending depends on balances, balances depend on payouts) never have to solve themselves within one day.

**1. Seed.** Trust has to start somewhere. The seed is built from the points members already hold, square-rooted so no single account dominates. Each day it drifts toward members with sustained, far-reaching participation, so the old points fade over time and the community's own record takes over.

```
s(next) = (1 − η)·s + η·normalize( min over the last N days of t·ρ·φ )
```

**2. Trust.** Personalized PageRank from the seed, over the last few days of who spent points on whom. P is each member's spending, normalized.

```
t ← (1 − a)·s + a·Pᵀ·t
```

Trust flows from the person engaging to the person they engage with. A farm that likes honest posts gains nothing from it. It only gains trust when honest members engage with *its* posts.

**3. Attention.** Only time with the tab visible and focused counts, and every second is split between whatever was open. A hundred waiting tabs each get a hundredth. An account can't log 127 hours of reading in a 24 hour day.

```
effective_dwell = ∫ 1/n(τ) dτ          n(τ) = tabs open at moment τ
ρ ← (1 − λ)·ρ + λ·min(effective_dwell ÷ read_estimate, 1)
```

ρ is a smoothed rating for the member, not a check on each click. One odd session barely moves it. A pattern does.

**4. Travel.** Follow a member's spending three steps out: where they spent, where those people spent, and where *those* people spent. The share that ends up outside the member's own circle is how far their points travel.

```
φ = Σ over accounts outside your circle of (P³)
```

A farm ring scores about 0.04. Honest members score 0.8 to 0.9.

**5. Member weight.**

```
w = t · ρ · φ
```

**6. Distance.** How far the engager is from the author in the graph of who engages with whom. 0 means a direct regular, approaching 1 for a stranger.

```
g(d) = 1 − e^(−(d − 1)/D)
```

**7. Engaging.** Pressing like v times costs v² points. Ten likes cost a hundred. The points are burned. Nothing goes to the author directly.

**8. Post score.** Engagement from regulars and from strangers is summed separately. The last term is zero unless both are present, so a post that gets both groups engaging scores highest.

```
I = Σ w·(1 − g)·√cost      engagement from regulars
O = Σ w·g·√cost            engagement from strangers
s = α·I + β·O + γ·√(I·O)   with α < β
```

**9. Payout.** A fixed daily pot is split by each post's share of the total score. Replies are scored the same way.

```
payout = M · s ÷ Σ s
```

**10. Flags.** A flag costs points. If a moderator upholds it, the points come back. If not, they're burned. There's no bonus for flagging, because paying people to flag creates a business of false flags. Flags are scored with the same regulars/strangers/both shape, and a high score sends the post to a moderator, never to an automatic penalty. An upheld flag costs the author trust and payout, and costs the people who spent heavily on that post a small amount of trust. The penalty stops there; it doesn't spread further through the graph.

**11. Balances.** Every balance shrinks a little each day (demurrage), so nobody can hoard points forever and the total supply settles at a steady level instead of growing.

```
B ← (1 − δ)·B + allowance + payout − spent ± flag outcomes
```

## Which safeguard does the work

Each run below switches one safeguard off, with flags disabled so the structural safeguards can be seen alone. The number is the share of all points paid out that was generated by farm engagement.

**Naive farm (many tabs open at once):**

| run | farm-generated share |
|---|---|
| all safeguards on | 0.1% |
| no reading-time check | 0.5% |
| no travel measure (φ) | 5.0% |
| neither | **82.1%** |

**Careful farm (one tab at a time, real reading time, so the time check is beaten):**

| run | farm-generated share |
|---|---|
| all safeguards on | 0.5% |
| no travel measure (φ) | **84.6%** |
| no trust | 0.7% |
| no regulars/strangers weighting | 0.6% |
| seed ignores old points | 0.8% |
| seed never drifts | 0.9% |
| spent points go to the author instead of burning | 0.0% |

The reading-time check and the travel measure each stop the naive farm on their own. Against the careful farm, the travel measure alone does it. Trust, burning, the regulars/strangers weighting and the choice of seed barely moved this particular number. They do other jobs: burning stops rings from passing points around, the weighting holds back the pod (below), and trust is how a newcomer's engagement gains weight. Those jobs still need scenarios built to test them.

### The mimic farm

What if the farm spends a third of its points on honest posts, to look normal? Its travel score jumps from 0.04 to 0.85, so it gets past φ. But to do that it has to send real points to real authors. The farm's own take *falls* from 8.8% to 7.7%, and the middle users' share rises from 6.1% to 8.9%, because the farm is now paying them. To look like a member, the farm has to act like one and pay for it.

## What's left

Once farms can't pay themselves, they only get what real readers give them. The simulation assumes readers like farm filler at some rate, and that's the shakiest assumption in the model, so it was varied:

| readers like filler… | structural only | with flags |
|---|---|---|
| 15% of the time | 2.5% | 0.6% |
| 30% of the time (default) | 8.9% | 2.5% |
| 30%, moderators only 60% accurate | | 3.5% |
| 45% of the time | 12.8% | 3.8% |

This is the content-farming gap. If real people like a post, the structural safeguards will pay its author. Only human judgment, through flags and moderators, separates filler from contribution. The system is only as good as its moderation.

### The pod

Twelve real people who agreed to engage with each other took **7.4%** of the payout with **2.0%** of the accounts, about 3.7 times their share of the population. Regular members of similar quality earned 1.2 times theirs, so the pod earned roughly three times the honest rate.

The regulars/strangers weighting is what holds them back. Pod members start out as strangers to each other, but repeated engagement makes them regulars in the graph. Without that weighting, the pod's share rose from 6.5% to 9.9%.

Real people coordinating is the attack this design doesn't stop. It makes it expensive: every member has to burn real points and give real attention. At that point it's hard to tell from a community.

### The jury

Engagements per member per day:

| | with daily allowance | without |
|---|---|---|
| novel posters | 3.38 | 3.15 |
| regular members | 3.01 | 1.49 |
| middle users | 2.61 | **0.42** |

Middle users earned 6.5% of the payout while making up a third of the accounts, which is what the design intends. But their reading and their likes are what tell the system a post is novel. If their only income is payouts from their own posts, they run dry and the jury goes quiet. The allowance keeps them engaged. It's safe because points spent by accounts nobody trusts don't move anything: the fake accounts got the same allowance and took 0.0% in every run.

## Attacks and what stops them

| attack | what stops it | in the simulation |
|---|---|---|
| like ring, many tabs | split reading time, travel measure | 0.1% self-generated |
| like ring, careful reading time | travel measure | 0.5% self-generated |
| ring pretending to be members | escaping its circle means paying honest authors | its take falls to 7.7% |
| swarm of fake accounts | no seed, no trust, no real reading time; the allowance buys nothing | 0.0% |
| filler that real readers like | flags with a cost, moderator review | 2.5%, depends on moderation |
| pod of real people | regulars/strangers weighting, burning, real attention | about 3× the honest rate |
| bought or hijacked trusted account | not addressed | not simulated |

## Where the pieces come from

Each component has precedent. As far as I've found, the combination aimed at engagement farming is new, and so are two of its parts: treating attention as something that can't be spent twice, and using distance in the graph as the measure of who's a regular and who's a stranger.

- **Costly signalling.** Spence (1973) in economics, Zahavi's handicap principle in biology. A signal is believable when faking it costs something.
- **Quadratic voting.** Weyl and Posner. Expressing intensity costs the square.
- **Seeded trust.** PageRank, EigenTrust, SybilGuard (2006), SybilRank (2012). Cheng and Friedman (2006) proved that a reputation system treating every account alike can't resist fake accounts, which is why the seed exists at all.
- **Demurrage.** Silvio Gesell, and the Wörgl scrip of 1932–33.
- **Bridging-based ranking.** Community Notes, Polis.
- **Distrust doesn't chain.** Guha et al. (2004). Hence flag penalties that stop after one hop.
- **Cautionary tales.** Steemit and Hive curation rewards (vote bots, voting circles), Slashdot moderation points, Medium claps.

## Open questions

- **The feed.** This design decides who gets paid, not what readers see. A feed sorted by score buries every new post. It needs deliberate room for new and low-scored posts.
- **Explaining it to members.** "Your like counted 0.3" will feel arbitrary. Members need some visible reason why their engagement weighs what it does.
- **Privacy.** Measuring focus and reading time is surveillance. Keep the rating and throw away the raw times.
- **Account takeover.** A trusted account that gets bought or hijacked brings its trust with it. Not simulated.
- **Parameters.** About fifteen, all set by hand and none tuned against real data.

### Limits of the simulation

Every attacker plays one fixed strategy, and none adapts. Honest behaviour is a simple rule: a reader likes a post with probability equal to its quality. Moderators are a weighted coin flip. No new members join. 593 accounts in six communities is small. The results show which mechanisms matter and roughly how much. They don't predict numbers on a real platform.

## Run it

The simulation is `sim.py`, a single Python file using only the standard library. Run it, change the numbers, try to break it.

```
python3 sim.py            # every scenario x 5 seeds, writes results.json (~90s on 20 cores)
python3 sim.py --quick    # one seed, shorter run, as a smoke test
```

Scenario names in the output:

- `s_*`: structural safeguards only (flags off), naive farm with many tabs open at once
- `c_*`: structural safeguards only, careful farm with real reading time, one tab at a time
- no prefix: flags on

"made-by-farm" is the share of all points paid out that was generated by farm engagement, wherever it landed. `run.log` holds the table from the run quoted above, and `results.json` the full numbers.
