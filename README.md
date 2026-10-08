# TruScore

**A cross-silo DeFi credit & solvency oracle on GenLayer.**

TruScore lets institutions with skin in the game vouch for a borrower's solvency on-chain. Staked
*evaluators* have an LLM-backed validator network turn off-chain financial data into a **Solvency
Score** (`0` = guaranteed default, `1000` = risk-free). Whitelisted *lenders* register each loan on-chain
against that score and pay the evaluator a fee. If the borrower defaults, the lender is paid directly from
the evaluator's stake (after a dispute window), so the evaluator is financially liable for every loan its
score enabled.

```
contracts/truscore_oracle.py   the oracle (GenVM / Python)
tests/test_truscore.py         230 direct-mode tests, including negative tests for every exploit class
deploy/deployScript.ts         `genlayer deploy` script (derives the network fee deposit)
```

> ## Operational requirements (read before deploying)
>
> 1. **The Governor MUST be a multisig and/or timelock - never a single EOA.** The governor whitelists
>    lenders and evidence domains and routes the treasury. The default-dispute window and the loan registry
>    bound what a malicious lender can do, but they do not remove the need to trust whoever controls the
>    whitelist. Rotate nothing through a hot key.
> 2. **`claim_treasury` destinations MUST be an insurance pool or a multisig** that pays out to harmed
>    lenders / evaluators. The contract cannot verify this on-chain (it only refuses the governor and the zero
>    address); doing so would require an explicit insurance contract. Treat the destination as a governance
>    decision and publish every payout.
> 3. **Seed the evidence-domain whitelist before use.** A fresh deployment has *no* allowed domains, so no
>    score can be requested until the governor calls `whitelist_domain` for each trusted credit API or IPFS gateway.

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
   +--------------------------+  |      evaluator: dispute_default(evidence)
      |                 |        |            |  LLM verifies the default
 no challenge     challenge_score(counter_url, + bond)
 finalize_score         v        |      confirmed / silent        refuted
      |        +------------------------+      -> finalize_default     -> claim cancelled,
      |        | DISPUTED               |         (slash + pay lender)     lender de-listed
      |        | resolve_dispute:       |
      |        |  median of 3 (or 1)    |
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
| **Governor** | Manages the lender and evidence-domain whitelists and routes the **treasury** via `claim_treasury`. Must be a multisig/timelock. It can never touch evaluator stake or default proceeds, cannot be a lender, and cannot send treasury funds to itself. |
| **Validators** | GenLayer consensus: the leader runs the LLM analysis, validators re-run it, accept a score within +/-100, and agree on failures they independently reproduce. |

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
| `VALIDATOR_TOLERANCE` | 100 | L1 consensus tolerance on a score. |
| `OVERTURN_THRESHOLD` | 250 | `2 x tolerance + 50`: a dispute overturns only beyond this difference. |
| `DISPUTE_PASSES` | 3 | Independent analyses in a median dispute. |
| `DISPUTE_TIMEOUT` | 7 days | After this, persistent transient evidence failures count against the evaluator. |

---

## 2. Game theory

The protocol is designed so that the only profitable behaviour is honest evaluation.

- **Evaluators are paid for, and liable for, the risk they take.** A score commits *capacity*
  (`score x stake / 1000`) against the evaluator's stake; each loan a lender registers is drawn from that
  capacity, **pinned** as exposure, and earns the evaluator an upfront **fee** (`record_loan(..., fee_amount)`,
  withdrawable immediately, never staked, never refunded). A confirmed default is paid from the stake.
- **Exposure is conserved.** Capacity plus open loans across all of an evaluator's scores may never exceed
  its stake, so one stake cannot back unlimited loans.
- **Liability attaches to loans, not to scores.** An evaluator cannot escape by lowering, replacing or letting
  a score expire, and a loan longer than 30 days stays protected for `LOAN_TTL`.
- **A default needs the evaluator's silence or the LLM's confirmation.** A lender's `report_default` only
  starts a 3-day window; the stake is untouched. A false claim costs the lender its whitelisting, so a rogue
  or compromised lender cannot profit from a fabricated default.
- **Lenders are independent.** One lender settling (or losing) its claim does not touch another's loan.
- **Evaluators cannot be held hostage by a lender.** `request_loan_closure` starts a 7-day countdown; if the
  lender has not reported a default by then, anyone can close the loan and the stake is freed.
- **Nobody can squat a borrower.** Any evaluator may replace a score with no borrow power, or outbid a live
  one with strictly greater borrow power, provided nothing has been lent against the current score. A dust
  score therefore cannot lock a borrower out for 30 days.
- **Honest scores are never punished for LLM noise.** Validators tolerate +/-100, so an honest original and
  an honest re-run can differ by up to 200. A dispute only overturns beyond 250, and the default re-run is the
  **median of three** independent analyses, so one outlier run cannot slash anyone.
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

`report_default(borrower)` **never slashes immediately**. It moves the caller's own loan to `PENDING_DEFAULT`
and keeps its exposure locked. The backing evaluator then has `DEFAULT_DISPUTE_WINDOW` (3 days) to call
`dispute_default(borrower, lender, evidence_url)`:
- the evidence (on a whitelisted domain) is judged by an LLM;
- **default confirmed**, or the evaluator's evidence is gone (404/410): the default is settled at once - exactly
  that loan's amount is taken from the evaluator (active stake first, then the unbonding queue, capped at what
  remains, first come first served), removed from its exposure and paid directly to the lender;
- **default refuted**: the claim is cancelled, the loan closed with no slash, and the **lender is removed from
  the whitelist**;
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
  **no loan is open against it** (the dust-score escape hatch). A score with no borrow power left (zero score,
  fully lent, stake gone) can be replaced immediately by anyone. Open loans are never affected by replacement.
- **Under-collateralisation is bounded:** if a dispute slash leaves stake below exposure, default payouts are
  paid first come, first served, capped at what the evaluator still holds.

### Evidence-domain whitelist
`whitelist_domain` / `remove_domain` (governor) maintain a set of allowed **exact hostnames** (e.g. a trusted
credit API or an IPFS gateway). The evaluator's `data_url`, the challenger's `counter_url` and the
`evidence_url` of a default dispute must all be on it (look-alikes such as `trusted.com.evil.org` or
`evil.trusted.com` are rejected). The whitelist is enforced when a URL is *accepted*; it is deliberately not
re-checked while a dispute is being resolved, so removing a domain can never freeze an open dispute.

### Strict unbonding queue
`initiate_unstake` moves stake into a 7-day queue (borrow power from live stake drops immediately). **Unbonding
funds remain slashable.** An evaluator with any `DISPUTED` score can neither start an unstake nor claim a
matured one.

### Median-based LLM consensus and validator agreement on failures
- L1: validators re-run the analysis and accept a leader score within +/-100.
- L2 (dispute): `resolve_dispute(borrower, use_median)`. `use_median = true` runs **three independent
  `run_nondet` analyses and takes the median**; `use_median = false` runs **one** analysis to stay inside
  network gas / time limits (noisier - prefer `true` whenever the network allows it).
- If the **leader reports an evidence error**, each validator re-runs the fetch itself and **agrees only if it
  hits the same class of error** (`[DATA_UNAVAILABLE]` or `[TRANSIENT]`), so a failure can reach consensus
  instead of hanging. Malformed-LLM or unclassified leader errors are rejected to force rotation.
- The overturn threshold (250) is wider than twice the L1 tolerance (200), so consensus noise can never slash an
  honest evaluator. LLM output is parsed defensively and prompts treat all fetched data as untrusted.

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
the unlent capacity and `score x live stake / 1000` less what is already lent.

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
| `resolve_dispute(borrower, use_median)` | anyone | Median-of-3 (`true`) or single (`false`) re-analysis over both sources, or evidence slash. -> `FINAL`. |
| `finalize_score(borrower)` | anyone | `PENDING` -> `FINAL` after 24h unchallenged. |
| `release_expired(borrower)` | anyone | Frees the *unlent* capacity of a score older than 30 days; loans stay pinned. |
| `record_loan(borrower, amount, fee_amount)` | whitelisted lender (payable) | Registers a loan; `msg.value` must equal `fee_amount` (> 0), credited to the evaluator's withdrawable balance. |
| `close_loan(borrower)` | the lender | Marks an open loan repaid and frees the exposure. |
| `request_loan_closure(borrower, lender)` | backing evaluator | Starts the 7-day countdown on an open loan. |
| `expire_loan(borrower, lender)` | anyone | Closes an open loan older than `LOAN_TTL`, or whose closure countdown has elapsed. |
| `report_default(borrower)` | the lender with an open loan | Moves its loan to `PENDING_DEFAULT` (no slash). |
| `dispute_default(borrower, lender, evidence_url)` | backing evaluator | Within 3 days: LLM verifies; confirmed -> settle, refuted -> cancel + de-list lender. |
| `finalize_default(borrower, lender)` | anyone | Settles an undisputed default after the window. |
| `initiate_unstake(amount)` | evaluator | Stake -> 7-day unbonding queue (guarded by exposure and disputes). |
| `claim_unstaked()` | evaluator | After 7 days, credit queued funds. |
| `withdraw()` | anyone | Pull credited balances (fees, payouts, claimed unstakes). |
| `whitelist_lender` / `remove_lender` | governor | Lender whitelist (the governor itself is not allowed). |
| `whitelist_domain` / `remove_domain` | governor | Evidence-host whitelist (exact hostnames). |
| `claim_treasury(amount, destination)` | governor | Insurance payout of forfeited bonds / dispute slashes; destination must be an insurance pool or multisig (see top of this file). |
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

After deploying, the governor must `whitelist_domain` for every evidence host and `whitelist_lender` for every
lender before the contract is usable.

Hosted Studio networks have no on-chain fee manager, so the script derives the fee deposit with
`estimateTransactionFees`; a bare `genlayer deploy --contract ...` is rejected with `FeeValueMustBeNonZero(1)`.

> **Toolchain note.** The contract pins runner `py-genlayer:5jycge4q...`. GenVM `v0.6.0-rc9` no longer
> ships that runner, so the linter and test harness must be pinned to `v0.6.0-rc8` (or earlier) with
> `GENVM_VERSION` until the pin is moved to a runner that rc9 provides.

The suite covers registration, SSRF and domain-whitelist rejection (data, counter and evidence URLs), finalization,
overturned / upheld disputes (exact threshold boundaries), median and single-run voting, counter-evidence,
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
- **Gas / time limits.** A median dispute makes three LLM calls (each fetching one or two documents) inside one
  transaction. If that exceeds the network's limits, call `resolve_dispute(borrower, false)` for a single pass;
  the 250-point overturn threshold already tolerates the extra noise.
- **Atomicity.** State-changing functions check before they write, so a failed call leaves no partial state even
  in simulators that do not roll back on revert.

---

## 7. Deployment

| | |
|---|---|
| Network | GenLayer Studio Devnet (`studio-dev`, chain id 61997) |
| Contract | `0x5151A27F483451B09812d909EA9A0E1D1A3eC19d` (current code) |
| Governor | `0x6Ec5cb7469a661B8E23B4867359893A25116eA19` (the deployer, a single EOA - **development only**) |
| Whitelisted domains | `api.example.com` (added by `deploy/deployScript.ts` right after deployment) |
| Previous revision | `0x559E42702E90C1c88878771a94e1f1765E6712Ba` (superseded) |

`genlayer deploy` runs `deploy/deployScript.ts`, which deploys the contract and then calls `whitelist_domain`
as the governor so the contract can serve score requests immediately. Change `INITIAL_DOMAIN` in the script
for a real data source; the governor should be a multisig for anything beyond a devnet.

---

## 8. Known limitations

- **The governor is still a trusted role.** The loan registry, the default dispute window, de-listing of lenders
  and the domain whitelist bound what a rogue lender can do, but a malicious governor can whitelist lenders and
  domains of its choosing. Run it as a multisig/timelock. There is no governor rotation.
- **The default verdict rests on the evaluator's evidence.** `dispute_default` takes the evaluator's evidence URL
  (restricted to whitelisted domains); the lender does not submit counter-evidence. Whitelist only sources an
  evaluator cannot forge. A transient evidence error inside the 3-day window can cost an evaluator its window.
- `claim_treasury` is discretionary and its destination cannot be verified on-chain (see the operational
  requirements). It can only move forfeited bonds and dispute slashes, never stake or default proceeds.
- DNS names aren't resolved (see *Known network constraints*).
- Loans are protected for `LOAN_TTL` (365 days); a lender that never calls `close_loan` keeps an evaluator's
  stake pinned until the evaluator requests closure and 7 days pass.
- Closing a loan returns the evaluator's exposure but not the borrower's capacity; a repaid borrower may need a
  fresh score (immediately replaceable once their capacity is exhausted, or outbid when nothing is lent).
- A lender that lends without first calling `record_loan` is unprotected by design.
- Liability ends once an evaluator's funds have fully left the contract after the unbonding period.
- A score is only as good as its LLM and data source; the median and thresholds bound noise, not bias in the
  source data.
