# TruScore

Cross-Silo DeFi Credit & Solvency Oracle on GenLayer.

Staked evaluators (institutions) request an LLM-derived **Solvency Score**
(0 = guaranteed default, 1000 = risk-free) for a borrower from off-chain
financial data. Scores are optimistic: anyone can challenge one within 24 hours
by posting a bond, and a Layer-2 re-run of the AI analysis settles the dispute.
If a finalized borrower defaults, the evaluator's stake is slashed to a treasury
that compensates lenders.

## Lifecycle

```
request_score_update -> PENDING --(24h, no challenge)--> finalize_score -> FINAL
                           |
                  challenge_score (+bond)
                           v
                       DISPUTED --resolve_dispute--> FINAL
```

| Function | Notes |
|---|---|
| `register_evaluator(amount)` | Payable; `amount >= min_stake` (default 100 GEN) and must equal value sent. |
| `request_score_update(borrower, data_url)` | Staked evaluators only. `data_url` passes the SSRF filter. Creates a `PENDING` score. |
| `challenge_score(borrower)` | Payable; exactly `challenge_bond` (default 5 GEN), within 24h; not by the evaluator. |
| `resolve_dispute(borrower)` | Re-runs the analysis. **Overturned** (diff > 150): evaluator slashed `min_stake // 2` to treasury, challenger credited bond + reward (= bond, from evaluator stake); new score `FINAL`. **Upheld** (diff <= 150): bond goes to treasury; original score `FINAL`. Slashes are capped by what the evaluator still holds (active + unbonding). |
| `finalize_score(borrower)` | Anyone, after the 24h window with no challenge. |
| `report_default(borrower, loss_amount)` | `FINAL` scores only. Slashes `min(stake, loss)` to treasury, score -> 0. |
| `initiate_unstake(amount)` | Moves stake from *active* to the **unbonding queue** for 7 days. Active stake, and so `get_max_borrow_power`, drops immediately; below `min_stake` no *new* scores can be requested. A further call adds to the queued amount and restarts the timer for the whole balance. Blocked while the caller is the evaluator of any `DISPUTED` score. |
| `claim_unstaked()` | After `unlock_time` (7 days), credits the unbonding balance to the caller's claimable balance. Also blocked while the caller has any `DISPUTED` score. |
| `distribute_treasury(lender, amount)` | Governor only (the deployer). Moves `amount` from the treasury to the lender's claimable balance. |
| `withdraw()` | Pull payment of everything credited to the caller (challenger payouts, claimed unstakes, treasury distributions). |
| `get_max_borrow_power(borrower)` | `(score * evaluator_stake) // 1000`. |

## Security

- `_is_safe_url` allows only literal `http://` / `https://`, rejects credentials,
  `localhost`/`.local`/`.internal`, and any non-global IP (loopback, link-local
  `169.254.x.x`, private, reserved, multicast, IPv4-mapped IPv6), including
  obfuscated decimal/hex hosts. It is enforced on request and again before every fetch.
- Payouts use a pull pattern; state is updated before any transfer is emitted.
- **Slashable unbonding.** Unstaking does not escape liability: `report_default` and
  `resolve_dispute` slash active stake first and then the unbonding queue. An evaluator
  cannot front-run a default or an overturn by unstaking. Because the timer is 7 days and the
  challenge window is 24h, an unstake started on a `PENDING` score stays slashable through a dispute.
- **Dispute lock.** An evaluator with a `DISPUTED` score can neither start an unstake nor claim a
  matured one until every such dispute is resolved, so a dispute left unresolved past 7 days cannot
  be outwaited.
- Zero-wei accounting invariant, checked in tests after every scenario:
  `total_deposited == total_staked + total_unbonding + total_escrow + treasury + total_claimable + total_withdrawn`.

## Known limitations

- DNS names are not resolved, so a hostname that resolves to a private IP is not caught here.
- Liability for a score ends once the evaluator's stake has left the contract. A default reported
  more than 7 days after the evaluator started unstaking (and after they claimed) cannot reach
  those funds, so lenders should size loan terms against the unbonding period.
- The governor is a single address with no rotation; treasury payouts are at its discretion.

## Dev

```
genvm-lint check contracts/truscore_oracle.py
pytest tests/ -v          # direct mode; needs genlayer-test (gltest)
```
