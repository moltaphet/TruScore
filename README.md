# TruScore

**A cross-silo DeFi credit & solvency oracle on GenLayer.**

TruScore lets institutions with skin in the game vouch for a borrower's solvency on-chain. Staked
*evaluators* have an LLM-backed validator network turn off-chain financial data into a **Solvency
Score** (`0` = guaranteed default, `1000` = risk-free) that validators reduce to an agreed **credit tier**; only the tier controls borrowing capacity. Whitelisted *lenders* register each loan on-chain
against that score and pay the evaluator a fee. If the borrower defaults, the lender is paid directly from
the evaluator's stake (after a dispute window), so the evaluator is financially liable for every loan its
score enabled.

```
contracts/truscore_oracle.py   the oracle (GenVM / Python)
tests/test_truscore.py         284 direct-mode tests, including adversarial validator-consensus tests for the agreed-bucket design
deploy/deployScript.ts         `genlayer deploy` script (derives the network fee deposit)
```

> ## Operational requirements (read before deploying)
>
> 1. **The Governor MUST be a multisig - never a single EOA.** The governor whitelists lenders and evidence
>    domains and routes the treasury. Lender whitelisting and treasury payouts are **timelocked on-chain (3
>    days)** and visible on-chain before it takes effect, but the delay does not replace multisig control. Rotate nothing through a hot key.
> 2. **Treasury payout destinations MUST be an insurance pool or a multisig** that pays out to harmed
>    lenders / evaluators. The contract cannot verify this on-chain (it only refuses the governor and the zero
>    address); doing so would require an explicit insurance contract. Every payout is first announced with
>    `propose_claim_treasury` and can only execute 3 days later - watch the proposals.
> 3. **Seed the evidence-domain whitelist before use.** A fresh deployment has *no* allowed domains, so no
>    score can be requested until the governor calls `whitelist_domain` for each trusted credit API or IPFS gateway.

---

## Deterministic Economic Consensus (Agreed Buckets)

**Steward finding addressed.** The earlier design let validators accept a leader score within +/-100 of their
own run, while borrowing capacity was computed *continuously* from the accepted score. Two validator-compatible
outputs (say 660 and 760) could therefore both pass consensus and yield materially different credit allocations.

**Fix.** The consensus value is no longer a number with a tolerance - it is a **tier** (bucket), and capacity is
a pure function of that tier.

| Tier | Score range | Borrow capacity (share of the evaluator's stake) | Capacity @ 200 GEN stake |
|---|---|---|---|
| 0 | 0 - 499 | 0% | 0 GEN |
| 1 | 500 - 699 | 50% | 100 GEN |
| 2 | 700 - 849 | 80% | 160 GEN |
| 3 | 850 - 1000 | 100% | 200 GEN |

Tiers are contiguous and non-overlapping (`TIER_FLOORS = (0, 500, 700, 850)`,
`TIER_CAPACITY_BPS = (0, 5000, 8000, 10000)`); the table is readable on-chain via `get_tier_table()` and
`get_tier_for_score(score)`.

1. **The leader/validator payload carries the tier, not the score.** `_analyze` maps the LLM score to a tier with
   the deterministic `_tier_of` and returns `{"tier", "reasoning"}`; the raw score never leaves the run.
2. **Exact bucket equality.** A validator accepts a leader output iff its *own* independent run resolves to the
   **same tier**. A leader at 700 (tier 2) is rejected by a validator at 699 (tier 1) although the scores differ
   by one - the old rule accepted it and the capacities differed by 30% of the stake. Malformed payloads
   (missing/out-of-range/non-integer tier, legacy `score`-only payloads) are rejected.
3. **Capacity is bound strictly to the agreed bucket.** `capacity = TIER_CAPACITY_BPS[tier] x stake / 10000`
   is the only formula used in `request_score_update`, `_borrow_power` and dispute resolution. Stored
   records hold the tier; `get_score().score` is the canonical tier floor, never the leader's raw number.
4. **Therefore every validator-compatible output preserves the same borrowing outcome**: all validators that
   accept a leader output share its tier, and the tier fixes the capacity - zero economic drift.
5. **Disputes are tier-based.** A challenge overturns a score only when the re-run is `OVERTURN_TIER_GAP = 2`
   or more tiers away; one-tier disagreement (boundary noise) never slashes an honest evaluator.

The cost of exactness is that a leader whose run lands next to a boundary can be rejected by validators on the
other side; GenLayer then rotates the leader. That is intended: the protocol prefers a retry over letting two
different allocations pass.

---

## 1. Architecture

```
                 stake GEN                          whitelists / insurance payouts
        evaluator ---------> TruScoreOracle <------------------------------------ governor
            |                    ^   ^                                         (multisig)
 request_score_update            |   |  record_loan(+fee) / close_loan        whitelisted
            v                    |   +--------------------------------------- lender
   +--------------------------+  |      report_default -> PENDING_DEFAULT
   | PENDING  24h challenge   |  |            |
   |          window, 0 power |  |            v   3-day window
   +--------------------------+  |      evaluator: dispute_default(evidence)  (lender evidence given at report)
      |                 |        |            |  LLM verifies the default
 no challenge     challenge_score(counter_url, + bond)
 finalize_score         v        |      confirmed / silent        refuted
      |        +------------------------+      -> finalize_default     -> claim cancelled,
      |        | DISPUTED               |         (slash + pay lender)     lender de-listed
      |        | resolve_dispute:       |
      |        |  one consensus run     |
      |        |  over BOTH sources, or |
      |        |  evidence-slash        |
      |        +------------------------+
      v                 v        |
   +----------------------------------+
   | FINAL  borrow power for 30 days  |---> loans pinned for up to 365 days
   +----------------------------------+
```

| Component | Role |
|---|---|
| **Evaluator** | Stakes GEN (>= `min_stake`, default 100 GEN), requests scores, earns the loan fee, and is liable for the loans it enables. |
| **Challenger** | Anyone; posts a `challenge_bond` (default 5 GEN) and optional counter-evidence to dispute a `PENDING` score. |
| **Lender** | Governor-whitelisted protocol. Registers each loan with `record_loan` (paying the evaluator's fee) **before** any default can be reported, and settles only its own loan. |
| **Governor** | Manages the lender and evidence-domain whitelists and routes the **treasury** via the timelocked `propose_/execute_claim_treasury`. **Must be a multisig.** It can never touch evaluator stake or default proceeds, cannot be a lender and cannot send treasury funds to itself - but these are address checks only, so a single-key Governor could still use an alias address. Lender additions and treasury payouts therefore sit behind a **3-day timelock**: every proposal is visible on-chain before it takes effect. A multisig remains strictly required. |
| **Validators** | GenLayer consensus: the leader runs the LLM analysis, validators re-run it, accept a leader output only if it resolves to the **same credit tier** as their own run, and agree on failures they independently reproduce. |

### Key parameters

| Constant | Value | Meaning |
|---|---|---|
| `min_stake` / `challenge_bond` | 100 GEN / 5 GEN | Constructor arguments. |
| `CHALLENGE_WINDOW` | 24 h | Time to challenge a `PENDING` score. |
| `SCORE_TTL` | 30 days | A `FINAL` score grants *new* borrow power until `timestamp + 30 days`. |
| `LOAN_TTL` | 365 days | A recorded loan stays protected this long, independent of the score's expiry. |
| `DEFAULT_DISPUTE_WINDOW` | 3 days | Evaluator's window to contest a reported default. |
| `CLOSURE_WINDOW` | 7 days | Lender's window to report a default after the evaluator requests closure. |
| `UNBONDING_PERIOD` | 7 days | Delay before unstaked funds can be claimed. |
| `TIER_FLOORS` | `0, 500, 700, 850` | Lowest score of each credit tier (the agreed bucket). |
| `TIER_CAPACITY_BPS` | `0, 5000, 8000, 10000` | Borrow capacity of each tier, in basis points of the evaluator's stake. |
| `OVERTURN_TIER_GAP` | 2 | A dispute overturns only when the re-run is at least this many tiers away. |
| `TIMELOCK_DELAY` | 3 days | Delay between proposing and executing a lender whitelisting or treasury payout. |
| `DUST_THRESHOLD` | 10 GEN | A score with less borrow power than this (and no open loans) can be overwritten by anyone. |
| `DISPUTE_TIMEOUT` | 7 days | After this, persistent transient evidence failures count against the evaluator. |

---

## 2. Game theory

The protocol is designed so that the only profitable behaviour is honest evaluation.

- **Evaluators are paid for, and liable for, the risk they take.** A score commits *capacity*
  (`tier capacity x stake`, e.g. 80% of stake for tier 2) against the evaluator's stake; each loan a lender registers is drawn from that
  capacity, **pinned** as exposure, and earns the evaluator an upfront **fee** (`record_loan(..., fee_amount)`,
  withdrawable immediately, never staked, never refunded). A confirmed default is paid from the stake.
- **Exposure is conserved.** Capacity plus open loans across all of an evaluator's scores may never exceed
  its stake, so one stake cannot back unlimited loans.
- **Liability attaches to loans, not to scores.** An evaluator cannot escape by lowering, replacing or letting
  a score expire, and a loan longer than 30 days stays protected for `LOAN_TTL`.
- **A default needs the evaluator's silence or the LLM's confirmation.** A lender's `report_default` (with its
  own evidence URL) only starts a 3-day window; the stake is untouched. A false claim costs the lender its whitelisting, so a rogue
  or compromised lender cannot profit from a fabricated default.
- **Lenders are independent.** One lender settling (or losing) its claim does not touch another's loan.
- **Evaluators cannot be held hostage by a lender.** `request_loan_closure` starts a 7-day countdown; if the
  lender has not reported a default by then, anyone can close the loan and the stake is freed.
- **Nobody can squat a borrower.** Any evaluator may replace a score with no borrow power, or outbid a live
  one with strictly greater borrow power, provided nothing has been lent against the current score. A **dust**
  score (borrow power under 10 GEN, no open loans) can be overwritten by anyone immediately, even with a
  lower score, so it cannot lock a borrower out for 30 days.
- **Honest scores are never punished for LLM noise.** Validators agree on the tier, so an honest original and
  an honest re-run can straddle at most one boundary. A dispute only overturns at 2 or more tiers' distance, so
  consensus noise on the single dispute run cannot slash anyone.
- **Challengers are paid for being right and pay for being wrong.** A successful challenge returns the bond
  plus a reward equal to the bond, taken from the evaluator. A failed one forfeits the bond to the treasury.
  Counter-evidence must come from a whitelisted domain, so a challenger cannot flood the LLM with fabricated
  sources.
- **Hiding the evidence loses; faking an outage does not help.** `404`/`410`/empty evidence resolves the
  dispute for the challenger. Transient failures (`429`, `5xx`, network) never slash an honest evaluator: the
  call reverts so it can be retried, but if they persist for `DISPUTE_TIMEOUT` they count against the evaluator.
- **You cannot run.** Stake backing live capacity or open loans can't be unstaked, and what does leave goes
  through a slashable 7-day queue.

---

## 3. Security measures

### Lender whitelisting, the loan registry and the default dispute window
A **whitelisted lender must first call `record_loan(borrower, amount, fee_amount)`**, which is bounded by the
borrower's remaining borrow power and recorded as `loans[borrower][lender]`, with the backing evaluator and
creation time. The governor cannot whitelist itself, and a lender can be neither the evaluator nor the borrower.

`report_default(borrower, lender_evidence_url)` **never slashes immediately**. It moves the caller's own loan to
`PENDING_DEFAULT`, stores the lender's evidence URL and keeps the exposure locked. The backing evaluator then
has `DEFAULT_DISPUTE_WINDOW` (3 days) to call `dispute_default(borrower, lender, evaluator_evidence_url)`:
- **two-sided evidence:** both URLs must be on a whitelisted domain, and a single `run_nondet` prompt contains
  the lender's evidence (Source L) **and** the evaluator's (Source E), asking the LLM to judge impartially
  between them - the evaluator is no longer the only voice;
- **default confirmed**, or the evaluator's evidence is gone (404/410): the default is settled at once - exactly
  that loan's amount is taken from the evaluator (active stake first, then the unbonding queue, capped at what
  remains, first come first served), removed from its exposure and paid directly to the lender;
- **default refuted**, or the lender's own evidence is gone (404/410, so the claim is unsupported): the claim is
  cancelled, the loan closed with no slash, and the **lender is removed from the whitelist**;
- a transient evidence error or a malformed LLM answer reverts so the evaluator can retry inside the window.

If the evaluator stays silent, anyone calls `finalize_default` once the window has passed. Other lenders' loans
on the same borrower are never affected.

### Active exposure tracking
`request_score_update` reverts with `Exposure exceeds stake` if `active_exposure + new capacity > staked
amount`. All checks run before any state change. Further rules keep the commitment honest:
- **Unstake guard:** `initiate_unstake` reverts if the remaining active stake would drop below `active_exposure`
  (which includes open and pending-default loans).
- **Replacement rules:** a live score that still offers borrow power cannot be replaced or lowered before
  expiry. It *can* be replaced by **any** evaluator whose new borrow power is **strictly greater** *and* when
  **no loan is open against it**; and a **dust score** (borrow power below `DUST_THRESHOLD` = 10 GEN with no
  open loans) can be overwritten by anyone immediately, whatever the new power. A score with no borrow power left (zero score,
  fully lent, stake gone) can be replaced immediately by anyone. Open loans are never affected by replacement.
- **Under-collateralisation is bounded:** if a dispute slash leaves stake below exposure, default payouts are
  paid first come, first served, capped at what the evaluator still holds.

### Evidence-domain whitelist
`whitelist_domain` / `remove_domain` (governor) maintain a set of allowed **exact hostnames** (e.g. a trusted
credit API or an IPFS gateway). The evaluator's `data_url`, the challenger's `counter_url` and the
`lender_evidence_url` and `evaluator_evidence_url` of a default dispute must all be on it (look-alikes such as `trusted.com.evil.org` or
`evil.trusted.com` are rejected). The whitelist is enforced when a URL is *accepted*; it is deliberately not
re-checked while a dispute is being resolved, so removing a domain can never freeze an open dispute.

> **Note:** The default deployment script whitelists `raw.githubusercontent.com` STRICTLY for devnet testing and demonstration purposes. On mainnet, domains must be strictly controlled APIs where neither party can freely edit the content.

### Governor timelocks
The instant `whitelist_lender` and `claim_treasury` are gone. Lenders are added with
`propose_whitelist_lender(lender)` -> (3 days) -> `execute_whitelist_lender(lender)`, and treasury payouts with
`propose_claim_treasury(amount, destination)` -> (3 days) -> `execute_claim_treasury()` (one pending payout at a
time; `cancel_*` withdraws a proposal; the amount is re-checked against the treasury at execution). A rogue
governor's alias lender is therefore visible on-chain for three days before it can record a loan or report a
default. Evaluators must monitor `get_pending_lender` and `get_pending_treasury`. If a malicious alias or suspicious payout is proposed, they cannot instantly unstake due to unbonding rules, but the 3-day window allows them to report the malicious action to the community or governor before it executes. Removing a lender (`remove_lender`) stays instant
because it only reduces power.

### Strict unbonding queue
`initiate_unstake` moves stake into a 7-day queue (borrow power from live stake drops immediately). **Unbonding
funds remain slashable.** An evaluator with any `DISPUTED` score can neither start an unstake nor claim a
matured one.

### Median-based LLM consensus and validator agreement on failures
- L1: validators re-run the analysis and accept the leader output **only if it maps to the exact same credit tier** (see *Deterministic Economic Consensus*).
- L2 (dispute): `resolve_dispute(borrower)` makes a **single** `run_nondet` analysis (no 3x loop, to stay
  inside block gas limits) and relies on GenLayer's validator consensus for LLM variance. The two-tier
  overturn gap exceeds the worst honest disagreement (one tier), so noise alone cannot slash.
- If the **leader reports an evidence error**, each validator re-runs the fetch itself and **agrees only if it
  hits the same class of error** (`[DATA_UNAVAILABLE]` or `[TRANSIENT]`), so a failure can reach consensus
  instead of hanging. Malformed-LLM or unclassified leader errors are rejected to force rotation.
- The overturn gap (2 tiers) is wider than the worst honest boundary disagreement (1 tier), so consensus noise
  can never slash an honest evaluator. LLM output is parsed defensively and prompts treat all fetched data as untrusted.

### The 404 / evidence-slash trap and counter-evidence
During `resolve_dispute`, a `404`/`410` (or empty body) at the evaluator's `data_url` **resolves for the
challenger**: the evaluator is slashed `min_stake / 2` to the treasury, the challenger gets the bond back plus a
reward, and the score is set to `0`. Transient errors revert instead. `challenge_score(borrower, counter_url)`
stores optional counter-evidence; the L2 prompt carries **Source A** (evaluator) and **Source B** (challenger)
and asks for the score the conflicting evidence supports. An unreachable counter URL never penalises the evaluator.

### SSRF filter
`_is_safe_url` allows only literal `http://` / `https://`, rejects embedded credentials, `localhost` /
`.local` / `.internal`, and every non-global IP: loopback, link-local (`169.254.x.x`), private, reserved,
multicast, IPv4-mapped IPv6, plus obfuscated decimal / hex hosts. It runs at every entry point and again before
every fetch, in addition to the domain whitelist.

### Freshness
`get_max_borrow_power` returns `0` unless the score is `FINAL` and no more than 30 days old. It is the lesser of
the unlent capacity and the live tier capacity (`TIER_CAPACITY_BPS[tier] x live stake / 10000`) less what is already lent.

---

## 4. Zero-Wei Accounting

Every wei that enters the contract sits in exactly one bucket at all times:

```
total_deposited == total_staked + total_unbonding + total_escrow
                 + treasury + total_claimable + total_withdrawn
```

All slashes, rewards, refunds, **loan fees**, lender payouts and treasury claims are transfers *between*
buckets; nothing is created or rounded away. (Evaluator fees enter as deposits and are credited to
`total_claimable` in the same call.) The test-suite checks this invariant against an independent external
ledger after every step of every scenario, cross-checks the aggregate counters against per-account views and the
recorded outbound transfers, and separately checks that each evaluator's `active_exposure` equals its unlent
capacity plus its open loans.

---

## 5. Function reference

| Function | Access | Description |
|---|---|---|
| `register_evaluator(amount)` | anyone (payable) | Stake >= `min_stake`; sent value must equal `amount`. Top-ups allowed. |
| `request_score_update(borrower, data_url)` | staked evaluator | SSRF- and domain-checked analysis; commits capacity; creates a `PENDING` score (or replaces/outbids per the rules above). |
| `challenge_score(borrower, counter_url)` | anyone (payable) | Exactly `challenge_bond` within 24h; `counter_url` whitelisted or `""`; not the evaluator. -> `DISPUTED`. |
| `resolve_dispute(borrower)` | anyone | Single consensus re-analysis over both sources, or evidence slash. -> `FINAL`. |
| `finalize_score(borrower)` | anyone | `PENDING` -> `FINAL` after 24h unchallenged. |
| `release_expired(borrower)` | anyone | Frees the *unlent* capacity of a score older than 30 days; loans stay pinned. |
| `record_loan(borrower, amount, fee_amount)` | whitelisted lender (payable) | Registers a loan; `msg.value` must equal `fee_amount` (> 0), credited to the evaluator's withdrawable balance. |
| `close_loan(borrower)` | the lender | Marks an open loan repaid and frees the exposure. |
| `request_loan_closure(borrower, lender)` | backing evaluator | Starts the 7-day countdown on an open loan. |
| `expire_loan(borrower, lender)` | anyone | Closes an open loan older than `LOAN_TTL`, or whose closure countdown has elapsed. |
| `report_default(borrower, lender_evidence_url)` | the lender with an open loan | Moves its loan to `PENDING_DEFAULT` (no slash) and stores its evidence. |
| `dispute_default(borrower, lender, evaluator_evidence_url)` | backing evaluator | Within 3 days: an impartial LLM weighs both sides; confirmed -> settle, refuted -> cancel + de-list lender. |
| `finalize_default(borrower, lender)` | anyone | Settles an undisputed default after the window. |
| `initiate_unstake(amount)` | evaluator | Stake -> 7-day unbonding queue (guarded by exposure and disputes). |
| `claim_unstaked()` | evaluator | After 7 days, credit queued funds. |
| `withdraw()` | anyone | Pull credited balances (fees, payouts, claimed unstakes). |
| `propose_whitelist_lender` / `execute_whitelist_lender` / `cancel_whitelist_lender` | governor | Lender whitelisting behind a 3-day timelock (the governor itself is not allowed). |
| `remove_lender` | governor | Instant de-listing. |
| `whitelist_domain` / `remove_domain` | governor | Evidence-host whitelist (exact hostnames). |
| `propose_claim_treasury(amount, destination)` / `execute_claim_treasury()` / `cancel_claim_treasury()` | governor | Insurance payout of forfeited bonds / dispute slashes behind a 3-day timelock; destination must be an insurance pool or multisig (see top of this file). |
| views | | `get_max_borrow_power`, `get_score`, `get_loan`, `get_stake`, `get_exposure`, `get_loan_exposure`, `get_unbonding`, `get_claimable`, `get_accounting`, `get_treasury`, `is_lender`, `is_domain_allowed`. |

---

## 6. Development

```bash
# Lint (pin the GenVM toolchain; see note below)
GENVM_VERSION=v0.6.0-rc8 genvm-lint check contracts/truscore_oracle.py

# Direct-mode tests (needs genlayer-test / gltest)
GENVM_VERSION=v0.6.0-rc8 pytest tests/ -v

# Deploy to the network selected with `genlayer network`
# (runs deploy/deployScript.ts; constructor args: min_stake=100 GEN, challenge_bond=5 GEN)
genlayer deploy
```

After deploying, the governor must `whitelist_domain` for every evidence host and propose + (3 days later) execute
`whitelist_lender` for every lender before the contract is usable.

Hosted Studio networks have no on-chain fee manager, so the script derives the fee deposit with
`estimateTransactionFees`; a bare `genlayer deploy --contract ...` is rejected with `FeeValueMustBeNonZero(1)`.

> **Toolchain note.** The contract pins runner `py-genlayer:5jycge4q...`. GenVM `v0.6.0-rc9` no longer
> ships that runner, so the linter and test harness must be pinned to `v0.6.0-rc8` (or earlier) with
> `GENVM_VERSION` until the pin is moved to a runner that rc9 provides.

The suite covers registration, SSRF and domain-whitelist rejection (data, counter and evidence URLs), finalization,
overturned / upheld disputes (exact tier-gap boundaries), single-run disputes, **adversarial validator-consensus
tests** (borderline leader pushes a higher tier and is rejected, exact bucket equality for all 1001 scores x 4
tiers, malformed payloads, identical capacity across every validator-compatible output, zero economic drift), counter-evidence, two-sided default evidence, timelocks,
404 vs transient evidence errors (including the dispute timeout), validator agreement on leader errors (via
`run_validator`) for both scoring and default verification, lender access control, the loan registry
(multi-lender defaults, loans outliving the score, loan expiry and closure countdowns), the default dispute window
(confirmed, refuted, silent, transient, malformed, timing boundaries, fake-loan attack), evaluator fees,
dust-score outbidding, under-collateralised payouts, exposure caps, unstake guards, unbonding slashes, dispute
locks, the treasury route, and zero-wei accounting across end-to-end lifecycles.

### Known network constraints

- **SSRF protections are best-effort and static.** The contract filters literal hosts and IP forms and then
  restricts fetches to governor-whitelisted exact hostnames. It does **not** resolve DNS, so a whitelisted
  hostname that later resolves to a private address is not caught; only whitelist hosts you control or trust, and
  prefer content-addressed (IPFS) gateways. All fetches run inside `run_nondet`, so every validator makes the
  request itself - assume the target sees one request per validator per pass.
- **`UserError` consensus semantics.** With `gl.vm.run_nondet`, validators compare *errors* as well as values.
  TruScore therefore raises fixed, classified messages (`[DATA_UNAVAILABLE]`, `[TRANSIENT]`) and the validator
  function returns `True` on a leader error **only** if its own re-run produces the same class; any other leader
  error (malformed LLM output, unclassified) is rejected so the leader rotates. The error text is exposed as
  `.message` or `.data` depending on the SDK build, so the contract reads both. A change to those message
  prefixes breaks cross-validator agreement.
- **Gas / time limits.** Every dispute resolution is a single `run_nondet` (one LLM call over one or two
  documents); the earlier median-of-three loop was removed to stay inside block gas limits.
- **Atomicity.** State-changing functions check before they write, so a failed call leaves no partial state even
  in simulators that do not roll back on revert.

---

## 7. Deployment

| | |
|---|---|
| Network | GenLayer Studio Devnet (`studio-dev`, chain id 61997) |
| Contract (bucketed code) | `0x8331d963b25682e34afC4639B81026C818Ae87F8` - deployed, `raw.githubusercontent.com` whitelisted; on-chain tier-table verification still pending (see *Deployment status*) |
| Contract (previous, superseded +/-100 code) | `0x4e7fB453f647dCE178B19fEC660d8c6C87e1CbCa` - do not use; it still has the continuous-capacity flaw |
| Governor | `0x6Ec5cb7469a661B8E23B4867359893A25116eA19` (the deployer, a single EOA - **development only**) |
| Whitelisted domains | `raw.githubusercontent.com` (added by `deploy/deployScript.ts` right after deployment; devnet testing/demonstration only - see the note under *Evidence-domain whitelist*) |

`genlayer deploy` runs `deploy/deployScript.ts`, which deploys the contract and then calls `whitelist_domain`
as the governor so the contract can serve score requests immediately. Change `INITIAL_DOMAIN` in the script
for a real data source.

### Deployment status (agreed-bucket contract)

The bucketed contract is deployed on Studio Devnet at `0x8331d963b25682e34afC4639B81026C818Ae87F8` and the
deploy script whitelisted `raw.githubusercontent.com`. Two earlier attempts failed server-side (the devnet
database was out of disk, then in recovery mode). The final step of the script - reading the tier table back -
failed because the node returned `Can't reconnect until invalid transaction is rolled back` on every read, so the
on-chain table has **not** been verified yet. Re-check it once reads work:

```
genlayer call 0x8331d963b25682e34afC4639B81026C818Ae87F8 get_tier_table
genlayer call 0x8331d963b25682e34afC4639B81026C818Ae87F8 get_tier_for_score --args 699
```

`deploy/deployScript.ts` ends with **deterministic state verification**: it reads `get_tier_table()` and
`get_tier_for_score()` from the chain and throws unless the floors are `[0, 500, 700, 850]`, the capacities are
`[0, 5000, 8000, 10000]`, and every boundary score (0, 499, 500, 699, 700, 849, 850, 1000) maps to its tier. (Do not pass `--contract` to `genlayer deploy`: that
bypasses the script and its fee derivation and the chain rejects the deposit with `FeeValueMustBeNonZero(1)`.)

### Test run evidence

```
$ pytest tests -q
....................................................................     [100%]
284 passed in 24.19s

$ pytest tests -v -k "borderline or rejects_any or exactly_bucket or malformed or identical_capacity or zero_economic or every_boundary"
test_borderline_leader_cannot_push_borrower_into_higher_tier PASSED
test_validator_rejects_any_leader_tier_other_than_its_own[699-2] PASSED   # validator 699 vs forged tier 2
test_validator_rejects_any_leader_tier_other_than_its_own[849-3] PASSED
test_acceptance_is_exactly_bucket_equality_for_every_score PASSED         # 1001 scores x 4 tiers
test_malformed_leader_payloads_are_rejected[...] PASSED                   # 11 forged payload shapes
test_every_validator_compatible_output_yields_identical_capacity PASSED
test_zero_economic_drift_across_independent_validators PASSED             # accepting set == 700..849 exactly
test_borrow_capacity_is_bound_to_bucket_at_every_boundary[...] PASSED     # 8 boundary scores
```

Mutation check: replacing the validator's tier equality with a +/-100 style tolerance makes 13 of these
tests fail, so they do detect the original flaw. (Run with the working GenVM runtime pinned, e.g.
`GENVM_VERSION=v0.6.0-rc8 pytest tests`.)

### Testing `request_score_update`

Register an evaluator (`register_evaluator`, value >= 100 GEN), then call `request_score_update` with a
`data_url` on the whitelisted host. Sample (dummy borrower profile; the file must be published at this path,
for example in a public repo, before it is fetched):

```
borrower: 0x000000000000000000000000000000000000dEaD
data_url: https://raw.githubusercontent.com/truscore-demo/sample-data/main/borrower.json
```

```json
{"wallet": "0x000000000000000000000000000000000000dEaD", "repaid_loans": 12, "defaults": 0,
 "avg_collateral_ratio": 1.8, "account_age_days": 540}
```

```
genlayer write 0x4e7fB453f647dCE178B19fEC660d8c6C87e1CbCa request_score_update \
  --args 0x000000000000000000000000000000000000dEaD https://raw.githubusercontent.com/truscore-demo/sample-data/main/borrower.json
```

---

## 8. Known limitations

- **The governor is still a trusted role.** The loan registry, the default dispute window, de-listing of lenders
  and the domain whitelist bound what a rogue lender can do, but a malicious governor can whitelist lenders and
  domains of its choosing. Run it as a multisig/timelock. There is no governor rotation.
- **Default verdicts are LLM judgments over two URLs.** Both sides now supply evidence, but the verdict is only
  as good as the sources and the model; whitelist only hosts neither party can edit at will. A transient
  evidence error inside the 3-day window can cost an evaluator its window.
- **Treasury destination cannot be verified on-chain.** The timelock makes every payout visible three days
  ahead; it does not prove the destination is an insurance pool.
- DNS names aren't resolved (see *Known network constraints*).
- Loans are protected for `LOAN_TTL` (365 days); a lender that never calls `close_loan` keeps an evaluator's
  stake pinned until the evaluator requests closure and 7 days pass.
- Closing a loan returns the evaluator's exposure but not the borrower's capacity; a repaid borrower may need a
  fresh score (immediately replaceable once their capacity is exhausted, or outbid when nothing is lent).
- A lender that lends without first calling `record_loan` is unprotected by design.
- Liability ends once an evaluator's funds have fully left the contract after the unbonding period.
- A score is only as good as its LLM and data source; consensus and thresholds bound noise, not bias in the
  source data.

### Remaining trust assumptions

1. **Governor.** The timelocks make an alias lender or treasury drain visible three days ahead, but they do not
   let evaluators exit in that time (active exposure and the 7-day unbonding period block it). A single-key
   governor remains a trusted role and must be a multisig. Evaluators must monitor `get_pending_lender` and `get_pending_treasury`. If a malicious alias or suspicious payout is proposed, they cannot instantly unstake due to unbonding rules, but the 3-day window allows them to report the malicious action to the community or governor before it executes.
2. **Dispute judge.** The `dispute_default` verdict is an LLM judgment between two whitelisted sources; if both
   sources are unreliable or unavailable the outcome follows the 404 rules above.
