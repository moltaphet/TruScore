# TruScore

**A cross-silo DeFi credit & solvency oracle on GenLayer.**

TruScore lets institutions with skin in the game vouch for a borrower's solvency on-chain. Staked
*evaluators* have an LLM-backed validator network turn off-chain financial data into a **Solvency
Score** (`0` = guaranteed default, `1000` = risk-free). Whitelisted *lenders* register each loan on-chain
against that score. If the borrower defaults, the lender is paid directly from the evaluator's stake,
so the evaluator is financially liable for every loan its score enabled.

```
contracts/truscore_oracle.py   the oracle (GenVM / Python)
tests/test_truscore.py         162 direct-mode tests, including negative tests for every exploit class
deploy/deployScript.ts         `genlayer deploy` script (derives the network fee deposit)
```

---

## 1. Architecture

```
                 stake GEN                          whitelist / insurance payouts
        evaluator ---------> TruScoreOracle <------------------------------- governor
            |                    ^   ^
 request_score_update            |   |  record_loan / close_loan      whitelisted
            v                    |   +-------------------------------- lender
   +--------------------------+  |      report_default (pays that lender directly)
   | PENDING  24h challenge   |  |
   |          window, 0 power |  |
   +--------------------------+  |
      |                 |        |
 no challenge     challenge_score(counter_url, + bond)
 finalize_score         v        |
      |        +------------------------+
      |        | DISPUTED               |
      |        | resolve_dispute:       |
      |        |  median of 3 runs over |
      |        |  BOTH evidence sources |
      |        |  or evidence-slash     |
      |        +------------------------+
      v                 v        |
   +----------------------------------+
   | FINAL  borrow power for 30 days  |---> loans pinned for up to 365 days
   +----------------------------------+
```

| Component | Role |
|---|---|
| **Evaluator** | Stakes GEN (>= `min_stake`, default 100 GEN), requests scores, and is liable for the loans they enable. |
| **Challenger** | Anyone; posts a `challenge_bond` (default 5 GEN) and optional counter-evidence to dispute a `PENDING` score. |
| **Lender** | Governor-whitelisted protocol. Registers each loan with `record_loan` **before** any default can be reported, and settles only its own loan. |
| **Governor** | The deployer. Manages the lender whitelist and may route the **treasury** (forfeited bonds and dispute slashes) to harmed parties via `claim_treasury`. It can never touch evaluator stake or default proceeds, cannot be a lender, and cannot send treasury funds to itself. |
| **Validators** | GenLayer consensus: the leader runs the LLM analysis, validators re-run it and accept a score within +/-100 of their own, and agree on failures they independently reproduce. |

### Key parameters

| Constant | Value | Meaning |
|---|---|---|
| `min_stake` / `challenge_bond` | 100 GEN / 5 GEN | Constructor arguments. |
| `CHALLENGE_WINDOW` | 24 h | Time to challenge a `PENDING` score. |
| `SCORE_TTL` | 30 days | A `FINAL` score grants *new* borrow power until `timestamp + 30 days`. |
| `LOAN_TTL` | 365 days | A recorded loan stays protected this long, independent of the score's expiry. |
| `UNBONDING_PERIOD` | 7 days | Delay before unstaked funds can be claimed. |
| `VALIDATOR_TOLERANCE` | 100 | L1 consensus tolerance on a score. |
| `OVERTURN_THRESHOLD` | 250 | `2 x tolerance + 50`: a dispute overturns only beyond this difference. |
| `DISPUTE_PASSES` | 3 | Independent analyses in a dispute; the **median** decides. |
| `DISPUTE_TIMEOUT` | 7 days | After this, persistent transient evidence failures count against the evaluator. |

---

## 2. Game theory

The protocol is designed so that the only profitable behaviour is honest evaluation.

- **Evaluators are first-loss capital.** A score commits *capacity* (`score x stake / 1000`) against the
  evaluator's stake. Each loan a lender registers is drawn from that capacity and **pinned** as exposure
  until it is closed, defaulted or expires. A default is paid from the stake straight to that lender.
- **Exposure is conserved.** Capacity plus open loans across all of an evaluator's scores may never exceed
  its stake, so one stake cannot back unlimited loans.
- **Liability attaches to loans, not to scores.** Because exposure is tied to the registered loan, an
  evaluator cannot escape by lowering, replacing or letting a score expire, and a loan longer than 30 days
  stays protected for `LOAN_TTL`.
- **Lenders are independent.** One lender settling its default does not touch another lender's loan.
- **Honest scores are never punished for LLM noise.** Validators tolerate +/-100, so an honest original
  and an honest re-run can differ by up to 200. A dispute only overturns beyond 250, and the re-run is the
  **median of three** independent analyses, so one outlier run can never slash anyone.
- **Challengers are paid for being right and pay for being wrong.** A successful challenge returns the bond
  plus a reward equal to the bond, taken from the evaluator. A failed one forfeits the bond to the treasury.
  A challenger can submit **counter-evidence** that the L2 analysis weighs against the evaluator's data.
- **Hiding the evidence loses; faking an outage does not help.** A `404`/`410` (or empty) evidence URL
  resolves the dispute for the challenger. Transient failures (`429`, `5xx`, network) never slash an honest
  evaluator: the call reverts so the challenger can retry; but if they persist for `DISPUTE_TIMEOUT` they
  count against the evaluator, who has had a week to restore the evidence.
- **You cannot run.** Stake backing live capacity or open loans can't be unstaked, and what does leave goes
  through a slashable 7-day queue.

---

## 3. Security measures

### Lender whitelisting and the on-chain loan registry
`report_default` can no longer be used to name an arbitrary amount. A **whitelisted lender must first call
`record_loan(borrower, amount)`**, which is bounded by the borrower's remaining borrow power and recorded as
`loans[borrower][lender]`, with the backing evaluator and creation time. `report_default(borrower)` then
settles **only the caller's own loan**: exactly that amount is taken from the evaluator's stake, removed from
its `active_exposure`, and paid directly to the lender. Other lenders' loans on the same borrower stay intact.
The governor cannot whitelist itself, and a lender can be neither the evaluator nor the borrower.
`close_loan` (lender) and `expire_loan` (anyone, after `LOAN_TTL`) free the stake of repaid or abandoned loans.

### Active exposure tracking
`request_score_update` reverts with `Exposure exceeds stake` if `active_exposure + new capacity > staked
amount`, so 100 GEN cannot back 500 GEN of loans. Further rules keep the commitment honest:
- **Unstake guard:** `initiate_unstake` reverts if the remaining active stake would drop below `active_exposure`.
- **No score evasion:** a live score that still offers borrow power cannot be replaced, raised or lowered
  before expiry, and another evaluator can't overwrite it. A score with **no** borrow power left (zero score,
  fully lent, stake gone) can be replaced immediately by any evaluator, so a borrower is never locked out.
  Open loans are unaffected by replacement.
- **Under-collateralisation is bounded:** if a dispute slash leaves stake below exposure, default payouts are
  paid first come, first served, capped at what the evaluator still holds (active stake, then unbonding).

### Strict unbonding queue
`initiate_unstake` moves stake into a 7-day queue (borrow power from live stake drops immediately). **Unbonding
funds remain slashable**: `report_default` and `resolve_dispute` take active stake first, then the queue. An
evaluator with any `DISPUTED` score can neither start an unstake nor claim a matured one.

### Median-based LLM consensus and validator agreement on failures
- L1: validators re-run the analysis and accept a leader score within +/-100.
- L2 (dispute): **three independent `run_nondet` analyses over both evidence sources; the median decides.**
- If the **leader reports an evidence error**, each validator re-runs the fetch itself and **agrees only if it
  hits the same class of error** (`[DATA_UNAVAILABLE]` or `[TRANSIENT]`), so a failure can reach consensus on the
  real network instead of hanging. Malformed-LLM or unclassified leader errors are rejected to force rotation.
- The overturn threshold (250) is wider than twice the L1 tolerance (200), so consensus noise (e.g. 449 vs 600)
  can never slash an honest evaluator. LLM output is parsed defensively and prompts treat all fetched data as
  untrusted.

### The 404 / evidence-slash trap
During `resolve_dispute`, a `404`/`410` (or empty body) at the evaluator's `data_url` **automatically resolves
for the challenger**: the evaluator is slashed `min_stake / 2` to the treasury, the challenger gets the bond
back plus a reward, and the score is set to `0`. Transient errors revert instead (see Game theory).

### Counter-evidence
`challenge_score(borrower, counter_url)` stores an optional counter-evidence URL (SSRF-checked). The L2 prompt
carries **Source A** (evaluator) and **Source B** (challenger) and asks the model to report the score the
conflicting evidence supports. An unreachable counter URL never penalises the evaluator.

### SSRF filter
`_is_safe_url` allows only literal `http://` / `https://`, rejects embedded credentials, `localhost` / `.local` /
`.internal`, and every non-global IP: loopback, link-local (`169.254.x.x`), private, reserved, multicast,
IPv4-mapped IPv6, plus obfuscated decimal / hex hosts. It is enforced on every URL, before every fetch.

### Freshness
`get_max_borrow_power` returns `0` unless the score is `FINAL` and no more than 30 days old. It is the lesser of
the unlent capacity and `score x live stake / 1000` less what is already lent, so neither a stake top-up nor
repeated lending can inflate it.

---

## 4. Zero-Wei Accounting

Every wei that enters the contract sits in exactly one bucket at all times:

```
total_deposited == total_staked + total_unbonding + total_escrow
                 + treasury + total_claimable + total_withdrawn
```

All slashes, rewards, refunds, lender payouts and treasury claims are transfers *between* buckets; nothing is
created or rounded away. The test-suite checks this invariant against an independent external ledger after
every step of every scenario (disputes, evidence slashes, unbonding slashes, multi-lender defaults, treasury
claims), cross-checks the aggregate counters against per-account views and the recorded outbound transfers,
and separately checks that each evaluator's `active_exposure` equals its unlent capacity plus its open loans.

---

## 5. Function reference

| Function | Access | Description |
|---|---|---|
| `register_evaluator(amount)` | anyone (payable) | Stake >= `min_stake`; sent value must equal `amount`. Top-ups allowed. |
| `request_score_update(borrower, data_url)` | staked evaluator | SSRF-checked analysis; commits capacity; creates a `PENDING` score. |
| `challenge_score(borrower, counter_url)` | anyone (payable) | Pay exactly `challenge_bond` within 24h; `counter_url` optional (`""`); not the evaluator. -> `DISPUTED`. |
| `resolve_dispute(borrower)` | anyone | Median-of-3 over both sources, or evidence slash. -> `FINAL`. |
| `finalize_score(borrower)` | anyone | `PENDING` -> `FINAL` after 24h unchallenged. |
| `release_expired(borrower)` | anyone | Frees the *unlent* capacity of a score older than 30 days; loans stay pinned. |
| `record_loan(borrower, amount)` | whitelisted lender | Registers a loan against remaining borrow power; pins it to the evaluator's stake. |
| `close_loan(borrower)` | the lender | Marks its loan repaid and frees the exposure. |
| `expire_loan(borrower, lender)` | anyone | Frees a loan older than `LOAN_TTL`. |
| `report_default(borrower)` | the lender with an open loan | Settles only the caller's loan, paying it directly from the evaluator's stake. |
| `initiate_unstake(amount)` | evaluator | Stake -> 7-day unbonding queue (guarded by exposure and disputes). |
| `claim_unstaked()` | evaluator | After 7 days, credit queued funds. |
| `withdraw()` | anyone | Pull credited balances. |
| `whitelist_lender(addr)` / `remove_lender(addr)` | governor | Manage the lender whitelist (the governor itself is not allowed). |
| `claim_treasury(amount, destination)` | governor | Insurance payout of forfeited bonds / dispute slashes; not to the governor or zero address. |
| views | | `get_max_borrow_power`, `get_score`, `get_loan`, `get_stake`, `get_exposure`, `get_loan_exposure`, `get_unbonding`, `get_claimable`, `get_accounting`, `get_treasury`, `is_lender`. |

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

Hosted Studio networks have no on-chain fee manager, so the script derives the fee deposit with
`estimateTransactionFees`; a bare `genlayer deploy --contract ...` is rejected with `FeeValueMustBeNonZero(1)`.

> **Toolchain note.** The contract pins runner `py-genlayer:5jycge4q...`. GenVM `v0.6.0-rc9` no longer
> ships that runner, so the linter and test harness must be pinned to `v0.6.0-rc8` (or earlier) with
> `GENVM_VERSION` until the pin is moved to a runner that rc9 provides.

The suite covers registration, SSRF rejection (data and counter URLs), finalization, overturned / upheld
disputes (with exact threshold boundaries), median voting, counter-evidence, 404 vs transient evidence errors
(including the dispute timeout), validator agreement on leader errors (via `run_validator`), lender access
control, the loan registry (multi-lender defaults, loans outliving the score, loan expiry, closing),
under-collateralised payouts, exposure caps, replacement and unstake front-running, unbonding slashes, dispute
locks, the treasury route, and zero-wei accounting across end-to-end lifecycles.

### Deployment

| | |
|---|---|
| Network | GenLayer Studio Devnet (`studio-dev`, chain id 61997) |
| Contract | `0x559E42702E90C1c88878771a94e1f1765E6712Ba` |
| Governor | `0x6Ec5cb7469a661B8E23B4867359893A25116eA19` (the deployer) |
| Parameters | `min_stake` = 100 GEN, `challenge_bond` = 5 GEN |
| Revision | Current: loan registry, counter-evidence, error-classified disputes, `claim_treasury` |

The earlier revision at `0x781aA737336138010C2593787dC20EF2f4c7Dee1` (no loan registry) is superseded and should not be used.

---

## 7. Known limitations

- **The governor controls the lender whitelist.** The loan registry bounds what a lender can take to a real
  evaluator's remaining borrow power, and the governor itself cannot be a lender, but a malicious governor can
  still whitelist a second address that registers and "defaults" a loan. Run the governor as a multisig or
  timelock, and treat whitelisted lenders as trusted. There is no governor rotation.
- `claim_treasury` is discretionary: the governor chooses the recipient (never itself). It can only move
  forfeited bonds and dispute slashes, never stake or default proceeds.
- DNS names aren't resolved, so a hostname that resolves to a private IP is not caught by the filter.
- Loans are protected for `LOAN_TTL` (365 days) after they are recorded; a lender that never calls `close_loan`
  keeps an evaluator's stake pinned until anyone calls `expire_loan`.
- Closing a loan returns the evaluator's exposure but not the borrower's capacity; a repaid borrower may need a
  fresh score (immediately replaceable once their capacity is exhausted).
- A lender that lends without first calling `record_loan` is unprotected by design.
- Liability ends once an evaluator's funds have fully left the contract after the unbonding period.
- A score is only as good as its LLM and data source; the median and thresholds bound noise, not bias in the
  source data.
