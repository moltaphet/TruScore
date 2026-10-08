# TruScore

**A cross-silo DeFi credit & solvency oracle on GenLayer.**

TruScore lets institutions with skin in the game vouch for a borrower's solvency on-chain. Staked
*evaluators* have an LLM-backed validator network turn off-chain financial data into a **Solvency
Score** (`0` = guaranteed default, `1000` = risk-free). Lenders size loans against that score. If the
borrower defaults, the lender is paid directly from the evaluator's stake, so the evaluator is
financially liable for every score it issues.

```
contracts/truscore_oracle.py   the oracle (GenVM / Python)
tests/test_truscore.py         99 direct-mode tests, including negative tests for every exploit class
```

---

## 1. Architecture

```
                         stake GEN                    whitelist
                evaluator ---------> TruScoreOracle <---------- governor
                    |                    |   ^
        request_score_update             |   | report_default (pays lender directly)
                    v                    |   |
   +-----------------------------+       |   |
   | PENDING  (24h challenge     |       |  whitelisted lender
   |           window, 0 power)  |       |
   +-----------------------------+       |
        |                  |             |
   no challenge      challenge_score     |
   finalize_score    (+ bond)            |
        |                  v             |
        |        +-------------------+   |
        |        | DISPUTED          |   |
        |        | resolve_dispute:  |   |
        |        | median of 3 runs, |   |
        |        | or evidence slash |   |
        |        +-------------------+   |
        v                  v             |
   +-----------------------------------+ |
   | FINAL  - borrow power for 30 days |-+
   +-----------------------------------+
```

| Component | Role |
|---|---|
| **Evaluator** | Stakes GEN (>= `min_stake`, default 100 GEN), requests scores, and is liable for them. |
| **Challenger** | Anyone; posts a `challenge_bond` (default 5 GEN) to dispute a `PENDING` score. |
| **Lender** | Governor-whitelisted protocol that lends against `get_max_borrow_power` and reports defaults. |
| **Governor** | The deployer. Controls only the lender whitelist. It has **no access to any funds**. |
| **Validators** | GenLayer consensus: the leader runs the LLM analysis, validators re-run it and accept a score within +/-100 of their own. |

### Key parameters

| Constant | Value | Meaning |
|---|---|---|
| `min_stake` / `challenge_bond` | 100 GEN / 5 GEN | Constructor arguments. |
| `CHALLENGE_WINDOW` | 24 h | Time to challenge a `PENDING` score. |
| `SCORE_TTL` | 30 days | A `FINAL` score grants borrow power until `timestamp + 30 days`. |
| `UNBONDING_PERIOD` | 7 days | Delay before unstaked funds can be claimed. |
| `VALIDATOR_TOLERANCE` | 100 | L1 consensus tolerance on a score. |
| `OVERTURN_THRESHOLD` | 250 | `2 x tolerance + 50`: dispute overturns only beyond this difference. |
| `DISPUTE_PASSES` | 3 | Independent analyses in a dispute; the **median** decides. |

---

## 2. Game theory

The protocol is designed so that the only profitable behaviour is honest evaluation.

- **Evaluators are the first-loss capital.** A score's *exposure* (`score x stake / 1000`) is
  committed against the evaluator's stake, and a default is paid from that stake straight to the lender.
  Over-scoring a borrower costs the evaluator real GEN.
- **Exposure is conserved.** Total exposure across all of an evaluator's scores may never exceed its
  stake, so one stake cannot back unlimited loans.
- **Honest scores are never punished for LLM noise.** Validators tolerate +/-100, so an honest
  original and an honest re-run can differ by up to 200. A dispute only overturns beyond 250, and the
  re-run is the **median of three** independent analyses, so one outlier run can never slash anyone.
- **Challengers are paid for being right and pay for being wrong.** A successful challenge returns the
  bond plus a reward equal to the bond, taken from the evaluator. A failed one forfeits the bond to the
  protocol treasury. Spurious challenges are therefore unprofitable.
- **Hiding the evidence loses.** If the evaluator takes down the `data_url`, the dispute resolves for
  the challenger. Withholding data is strictly worse than defending the score.
- **You cannot run, and you cannot rewrite history.** Stake backing live scores can't be unstaked, a
  live score can't be replaced or lowered, and anything that does leave goes through a slashable 7-day queue.
- **Nobody can take the money.** The governor can't touch funds, the treasury has no payout path, and
  default proceeds go to the reporting lender, not to a protocol-controlled pool.

---

## 3. Security measures

### Lender whitelisting
`report_default` is callable **only by lenders the governor has whitelisted**, and a lender can be
neither the evaluator nor the borrower (preventing a self-dealt "default" that drains an evaluator).
The slash is paid **directly to the calling lender**, capped at
`min(loss_amount, committed borrow power)`. `distribute_treasury` was removed entirely, so there is no
governor path to funds.

### Active exposure tracking
`request_score_update` commits `score x stake // 1000` and reverts with `Exposure exceeds stake` if
`active_exposure + new exposure > staked amount`. This closes infinite leverage (100 GEN cannot back
500 GEN of loans). Two further rules keep the commitment honest:
- **Unstake guard:** `initiate_unstake` reverts if the remaining active stake would drop below
  `active_exposure`. Only stake that is not backing a live score can leave.
- **No score evasion:** a live score cannot be replaced, raised or lowered to 0 before expiry. It can
  be replaced only after 30 days, or once its liability is settled (defaulted / overturned to zero
  exposure). `PENDING` and `DISPUTED` scores can't be replaced at all, and another evaluator can't
  overwrite someone else's live score.

Exposure is released when a score is defaulted, overturned down, replaced after expiry, or via
`release_expired(borrower)` (callable by anyone once a score is more than 30 days old and not `DISPUTED`).

### Strict unbonding queue
`initiate_unstake` moves stake into an unbonding queue for 7 days (borrow power from the live stake
drops immediately). **Unbonding funds remain slashable**: `report_default` and `resolve_dispute` take
active stake first, then the queue. An evaluator with any `DISPUTED` score can neither start an unstake
nor claim a matured one, so a dispute can't be outwaited. A second unstake adds to the queue and
restarts the timer for the whole balance.

### Median-based LLM consensus
- L1: validators re-run the analysis and accept a leader score within +/-100.
- L2 (dispute): **three independent `run_nondet` analyses; the median score decides.**
- The overturn threshold (250) is deliberately wider than twice the L1 tolerance (200), so ordinary
  consensus noise (e.g. 449 vs 600) can never slash an honest evaluator.
- LLM output is parsed defensively (key aliasing, clamping to 0-1000), and prompts treat fetched data
  as untrusted. A malformed LLM answer **reverts** the call (retry later); it never slashes anyone.

### The 404 / evidence-slash trap
If, during `resolve_dispute`, the `data_url` is unreachable, returns a 4xx/5xx status, or is empty, the
dispute **automatically resolves for the challenger**: the evaluator is slashed `min_stake / 2` to the
treasury, the challenger gets the bond back plus a reward, and the score is set to `0`. An evaluator
cannot deadlock a dispute (and lock the challenger's bond) by pulling the evidence.

### SSRF filter
`_is_safe_url` allows only literal `http://` / `https://`, rejects embedded credentials,
`localhost` / `.local` / `.internal`, and every non-global IP: loopback, link-local (`169.254.x.x`),
private, reserved, multicast, IPv4-mapped IPv6, plus obfuscated decimal / hex hosts. It is enforced on
request **and again before every fetch**.

### Freshness
`get_max_borrow_power` returns `0` unless the score is `FINAL` and no more than 30 days old, so
`PENDING` and `DISPUTED` scores can't be borrowed against and stale scores expire. It is the lesser of
`score x live stake / 1000` and the exposure committed at issuance, so a later top-up can't inflate it.

---

## 4. Zero-Wei Accounting

Every wei that enters the contract sits in exactly one bucket at all times:

```
total_deposited == total_staked + total_unbonding + total_escrow
                 + treasury + total_claimable + total_withdrawn
```

All slashes, rewards, refunds and payouts are transfers *between* buckets; nothing is created or
rounded away. The test-suite checks this invariant against an independent external ledger after every
step of every scenario (including disputes, evidence slashes, unbonding slashes and lender payouts),
and cross-checks the aggregate counters against per-account views and the recorded outbound transfers.

---

## 5. Function reference

| Function | Access | Description |
|---|---|---|
| `register_evaluator(amount)` | anyone (payable) | Stake >= `min_stake`; sent value must equal `amount`. Top-ups allowed. |
| `request_score_update(borrower, data_url)` | staked evaluator | SSRF-checked analysis; commits exposure; creates a `PENDING` score. |
| `challenge_score(borrower)` | anyone (payable) | Pay exactly `challenge_bond` within 24h; not the evaluator. -> `DISPUTED`. |
| `resolve_dispute(borrower)` | anyone | Median-of-3 re-analysis or evidence slash. -> `FINAL`. |
| `finalize_score(borrower)` | anyone | `PENDING` -> `FINAL` after 24h unchallenged. |
| `release_expired(borrower)` | anyone | Frees exposure of a score older than 30 days. |
| `report_default(borrower, loss_amount)` | whitelisted lender | Slash and pay lender directly; score -> 0. |
| `initiate_unstake(amount)` | evaluator | Stake -> 7-day unbonding queue (guarded by exposure and disputes). |
| `claim_unstaked()` | evaluator | After 7 days, credit queued funds. |
| `withdraw()` | anyone | Pull credited balances. |
| `whitelist_lender(addr)` / `remove_lender(addr)` | governor | Manage the lender whitelist. |
| `get_max_borrow_power(borrower)` | view | See *Freshness*. Also: `get_score`, `get_stake`, `get_exposure`, `get_unbonding`, `get_claimable`, `get_accounting`, `get_treasury`, `is_lender`. |

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

### Deployment

| | |
|---|---|
| Network | GenLayer Studio Devnet (`studio-dev`, chain id 61997) |
| Contract | `0x781aA737336138010C2593787dC20EF2f4c7Dee1` |
| Governor | `0x6Ec5cb7469a661B8E23B4867359893A25116eA19` (the deployer) |
| Parameters | `min_stake` = 100 GEN, `challenge_bond` = 5 GEN |

> **Toolchain note.** The contract pins runner `py-genlayer:5jycge4q...`. GenVM `v0.6.0-rc9` no longer
> ships that runner, so the linter and test harness must be pinned to `v0.6.0-rc8` (or earlier) with
> `GENVM_VERSION` until the pin is moved to a runner that rc9 provides.

The suite covers registration, SSRF rejection, finalization, overturned / upheld disputes (with exact
threshold boundaries), median voting, the evidence trap, lender access control, exposure caps,
score expiry, replacement and unstake front-running, unbonding slashes, dispute locks, and zero-wei
accounting across end-to-end lifecycles.

---

## 7. Known limitations

- DNS names aren't resolved, so a hostname that resolves to a private IP is not caught by the filter.
- One default report per score: once a lender reports, the score is `0` and further reports revert.
- Borrow power (and default recourse) ends 30 days after issuance; loans that outlive the score are not protected.
- Treasury funds (dispute slashes, forfeited bonds) are intentionally unreachable: there is no payout path.
- The governor is a single address with no rotation and controls the lender whitelist; lenders are trusted.
- Liability ends once an evaluator's funds have fully left the contract after the unbonding period.
- A score is only as good as its LLM and data source; the median and thresholds bound noise, not bias in the source data.
