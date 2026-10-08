import json
from datetime import datetime, timedelta, timezone

import pytest

CONTRACT = "contracts/truscore_oracle.py"
GEN = 10**18
MIN_STAKE = 100 * GEN
BOND = 5 * GEN
URL = "https://data.example.com/borrower.json"
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
WEEK = timedelta(days=7)
TTL = timedelta(days=30)
OVERTURN_THRESHOLD = 250
EPS = timedelta(seconds=1)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


class Env:
    """Wraps the deployed contract plus an independent external ledger."""

    def __init__(self, vm, contract):
        self.vm = vm
        self.c = contract
        self.deposited = 0
        self.transfers = []  # (recipient, amount): withdraw() and direct lender payouts
        self.t = T0
        vm.warp(iso(T0))

    # -- clock ---------------------------------------------------------
    def warp(self, delta):  # absolute offset from T0
        self.t = T0 + delta
        self.vm.warp(iso(self.t))

    def advance(self, delta):  # relative to the current clock
        self.t = self.t + delta
        self.vm.warp(iso(self.t))

    # -- actions -------------------------------------------------------
    def stake(self, who, amount):
        self.vm.sender = who
        self.vm.value = amount
        try:
            self.c.register_evaluator(amount)
        finally:
            self.vm.value = 0
        self.deposited += amount

    def mock_score(self, score, reasoning="r"):
        self.vm.clear_mocks()
        self.vm.mock_web(r".*", {"status": 200, "body": '{"revenue": 100}'})
        self.vm.mock_llm(r".*Solvency Score.*", json.dumps(json.dumps({"score": score, "reasoning": reasoning})))

    def mock_passes(self, scores):
        self.vm.clear_mocks()
        self.vm.mock_web(r".*", {"status": 200, "body": '{"revenue": 100}'})
        for i, sc in enumerate(scores, 1):
            self.vm.mock_llm(
                rf".*Analysis pass {i} of {len(scores)}.*",
                json.dumps(json.dumps({"score": sc, "reasoning": f"pass{i}"})),
            )

    def mock_web_status(self, status, body=""):
        self.vm.clear_mocks()
        self.vm.mock_web(r".*", {"status": status, "body": body})
        self.vm.mock_llm(r".*Solvency Score.*", json.dumps(json.dumps({"score": 500, "reasoning": "x"})))

    def request(self, evaluator, borrower, score):
        self.mock_score(score)
        self.vm.sender = evaluator
        self.c.request_score_update(borrower, URL)

    def challenge(self, who, borrower, value=BOND):
        self.vm.sender = who
        self.vm.value = value
        try:
            self.c.challenge_score(borrower)
        finally:
            self.vm.value = 0
        self.deposited += value

    def resolve(self, borrower, new_score, caller=None):
        self.mock_score(new_score, "re-run")
        self.vm.sender = caller if caller is not None else self.rando
        self.c.resolve_dispute(borrower)

    def resolve_passes(self, borrower, scores):
        self.mock_passes(scores)
        self.vm.sender = self.rando
        self.c.resolve_dispute(borrower)

    def default(self, borrower, loss, lender=None):
        self.vm.sender = lender if lender is not None else self.lender
        self.c.report_default(borrower, loss)

    def withdraw(self, who):
        self.vm.sender = who
        return self.c.withdraw()

    def finalize(self, borrower):
        self.vm.sender = self.rando
        self.c.finalize_score(borrower)

    def assert_zero_wei(self, evaluators=()):
        a = self.c.get_accounting()
        assert a["total_deposited"] == self.deposited
        assert a["total_deposited"] == (
            a["total_staked"] + a["total_unbonding"] + a["total_escrow"]
            + a["treasury"] + a["total_claimable"] + a["total_withdrawn"]
        )
        assert sum(self.c.get_unbonding(e)["amount"] for e in evaluators) == a["total_unbonding"]
        assert sum(self.c.get_stake(e) for e in evaluators) == a["total_staked"]
        assert a["total_withdrawn"] == sum(amt for _, amt in self.transfers)
        assert a["treasury"] == self.c.get_treasury()

    def assert_exposure_invariant(self, evaluators, borrowers):
        """active_exposure per evaluator == sum of its records' exposure."""
        for e in evaluators:
            total = 0
            for b in borrowers:
                try:
                    sc = self.c.get_score(b)
                except Exception:
                    continue
                if sc["evaluator"] == e.as_hex:
                    total += sc["exposure"]
            assert self.c.get_exposure(e) == total


@pytest.fixture
def env(direct_vm, direct_deploy, monkeypatch):
    c = direct_deploy(CONTRACT, MIN_STAKE, BOND)
    e = Env(direct_vm, c)

    import genlayer.chain as chain_mod
    from genlayer import Address

    class RecordingAccount:
        def __init__(self, addr):
            self.addr = addr

        def emit_transfer(self, amount, **kwargs):
            e.transfers.append((self.addr, int(amount)))

    monkeypatch.setattr(chain_mod, "Account", RecordingAccount)

    e.governor = Address(c.get_governor())
    e.lender = Address(b"\x11" * 20)
    e.rando = Address(b"\x22" * 20)
    e.many = [Address(bytes([0x40 + i]) * 20) for i in range(8)]
    direct_vm.sender = e.governor
    c.whitelist_lender(e.lender)
    return e


def _addr(raw):
    from genlayer import Address

    return Address(raw)


@pytest.fixture
def alice(env, direct_alice):
    return _addr(direct_alice)


@pytest.fixture
def bob(env, direct_bob):
    return _addr(direct_bob)


@pytest.fixture
def carol(env, direct_charlie):
    return _addr(direct_charlie)


@pytest.fixture
def dave(env, direct_owner):
    return _addr(direct_owner)


def _finalize(env, evaluator, borrower, score):
    env.request(evaluator, borrower, score)
    env.advance(timedelta(hours=25))
    env.finalize(borrower)


# ======================================================================
# registration
# ======================================================================

def test_defaults_and_registration(env, alice):
    assert env.c.get_min_stake() == MIN_STAKE
    assert env.c.get_challenge_bond() == BOND
    env.stake(alice, 150 * GEN)
    assert env.c.get_stake(alice) == 150 * GEN
    env.stake(alice, MIN_STAKE)  # top-up
    assert env.c.get_stake(alice) == 250 * GEN
    env.assert_zero_wei([alice])


def test_registration_rejections(env, alice):
    env.vm.sender = alice
    env.vm.value = 10 * GEN
    with env.vm.expect_revert("Stake below minimum"):
        env.c.register_evaluator(10 * GEN)
    env.vm.value = MIN_STAKE - 1
    with env.vm.expect_revert("Sent value must equal amount"):
        env.c.register_evaluator(MIN_STAKE)


# ======================================================================
# SSRF
# ======================================================================

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x", "http://localhost/x", "http://sub.localhost/x",
    "http://169.254.169.254/latest/meta-data", "http://10.0.0.1/x",
    "http://192.168.1.1/x", "http://172.16.0.1/x", "http://0.0.0.0/x",
    "http://[::1]/x", "http://[::ffff:127.0.0.1]/x", "http://2130706433/x",
    "http://0x7f000001/x", "http://user:pw@example.com/x",
    "ftp://example.com/x", "file:///etc/passwd", "gopher://example.com",
    "HTTP://example.com/x", "javascript:alert(1)", "http:///x", "",
    "http://example.com/a b", "http://service.internal/x",
])
def test_ssrf_rejected(env, alice, bob, url):
    env.stake(alice, MIN_STAKE)
    env.mock_score(500)
    env.vm.sender = alice
    with env.vm.expect_revert("Unsafe data_url"):
        env.c.request_score_update(bob, url)


def test_ssrf_allows_public(env, alice, bob):
    env.stake(alice, MIN_STAKE)
    env.mock_score(500)
    env.vm.sender = alice
    env.c.request_score_update(bob, "http://93.184.216.34:8080/data")
    assert env.c.get_score(bob)["state"] == "PENDING"


def test_request_requires_evaluator(env, alice, bob):
    env.mock_score(500)
    env.vm.sender = bob
    with env.vm.expect_revert("not a staked evaluator"):
        env.c.request_score_update(alice, URL)


# ======================================================================
# finalization / challenge window
# ======================================================================

def test_unchallenged_finalization(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 800)
    s = env.c.get_score(bob)
    assert s["score"] == 800 and s["state"] == "PENDING" and s["challenger"] == ""
    assert s["timestamp"] == int(T0.timestamp())
    assert s["exposure"] == 160 * GEN

    env.vm.sender = carol
    with env.vm.expect_revert("Challenge window still open"):
        env.c.finalize_score(bob)

    env.warp(timedelta(hours=24))
    env.c.finalize_score(bob)
    assert env.c.get_score(bob)["state"] == "FINAL"
    with env.vm.expect_revert("not PENDING"):
        env.challenge(carol, bob)
    env.assert_zero_wei([alice])


def test_challenge_window_and_rules(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 800)
    env.warp(timedelta(hours=24))
    with env.vm.expect_revert("Challenge window closed"):
        env.challenge(carol, bob)
    env.vm.value = 0

    env.request(alice, env.many[0], 100)          # a fresh PENDING score (bob's is still live)
    with env.vm.expect_revert("own score"):
        env.challenge(alice, env.many[0])
    with env.vm.expect_revert("exactly the challenge bond"):
        env.challenge(carol, env.many[0], value=BOND - 1)
    env.vm.value = 0
    env.assert_zero_wei([alice])


# ======================================================================
# dispute: overturned / upheld (median + widened threshold)  [fix 5]
# ======================================================================

def test_dispute_overturned(env, alice, bob, carol):
    stake = 200 * GEN
    env.stake(alice, stake)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)
    s = env.c.get_score(bob)
    assert s["state"] == "DISPUTED" and s["challenger"] == carol.as_hex
    assert env.c.get_accounting()["total_escrow"] == BOND
    env.assert_zero_wei([alice])

    env.resolve(bob, 600)  # |600-900| = 300 > 250
    s = env.c.get_score(bob)
    assert s["state"] == "FINAL" and s["score"] == 600

    slash = MIN_STAKE // 2
    assert env.c.get_stake(alice) == stake - slash - BOND
    assert env.c.get_treasury() == slash
    assert env.c.get_claimable(carol) == 2 * BOND
    assert env.c.get_accounting()["total_escrow"] == 0
    env.assert_zero_wei([alice])

    assert env.withdraw(carol) == 2 * BOND
    assert env.transfers == [(carol, 2 * BOND)]
    env.assert_zero_wei([alice])
    with env.vm.expect_revert("Nothing to withdraw"):
        env.withdraw(carol)


def test_dispute_overturned_low_stake_caps_payouts(env, alice, bob, carol):
    env.stake(alice, MIN_STAKE)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)
    env.resolve(bob, 0)
    assert env.c.get_stake(alice) == MIN_STAKE - MIN_STAKE // 2 - BOND
    env.assert_zero_wei([alice])


@pytest.mark.parametrize("new_score", [900, 750, 650, 1000, 1049 - 1])
def test_dispute_upheld(env, alice, bob, carol, new_score):
    stake = 200 * GEN
    env.stake(alice, stake)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)
    env.resolve(bob, new_score)  # diff <= 250 (650 is exactly 250)

    s = env.c.get_score(bob)
    assert s["state"] == "FINAL" and s["score"] == 900  # original stands
    assert env.c.get_stake(alice) == stake
    assert env.c.get_treasury() == BOND
    assert env.c.get_claimable(carol) == 0
    env.assert_zero_wei([alice])


def test_overturn_boundary(env, alice, bob, carol, dave):
    env.stake(alice, 300 * GEN)
    env.request(alice, bob, 400)
    env.request(alice, dave, 400)
    env.challenge(carol, bob)
    env.challenge(carol, dave)
    env.resolve(bob, 400 + OVERTURN_THRESHOLD)       # diff 250 -> upheld
    assert env.c.get_score(bob)["score"] == 400
    env.resolve(dave, 400 + OVERTURN_THRESHOLD + 1)  # diff 251 -> overturned
    assert env.c.get_score(dave)["score"] == 651
    assert env.c.get_claimable(carol) == 2 * BOND
    env.assert_zero_wei([alice])


def test_validator_tolerance_noise_never_slashes(env, alice, bob, carol):
    """449 vs 600 (diff 151) is within 2*VALIDATOR_TOLERANCE + margin: upheld."""
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 600)
    env.challenge(carol, bob)
    env.resolve(bob, 449)
    s = env.c.get_score(bob)
    assert s["score"] == 600 and s["state"] == "FINAL"
    assert env.c.get_stake(alice) == 200 * GEN
    assert env.c.get_claimable(carol) == 0
    env.assert_zero_wei([alice])


def test_threshold_is_wider_than_two_validator_tolerances():
    assert OVERTURN_THRESHOLD > 2 * 100  # L1 tolerance: two honest runs can differ by 200


def test_dispute_uses_median_of_three(env, alice, bob, carol, dave):
    env.stake(alice, 300 * GEN)
    env.request(alice, bob, 900)
    env.request(alice, dave, 100)
    env.challenge(carol, bob)
    env.challenge(carol, dave)
    # one wild outlier cannot overturn: median(100, 890, 900) = 890
    env.resolve_passes(bob, [100, 890, 900])
    assert env.c.get_score(bob)["score"] == 900
    assert env.c.get_claimable(carol) == 0
    # two low passes do: median(100, 100, 900) -> 100... vs old 100? use 900 original
    env.assert_zero_wei([alice])
    # outlier in the other direction on a low original score
    env.resolve_passes(dave, [900, 120, 110])
    assert env.c.get_score(dave)["score"] == 100
    env.assert_zero_wei([alice])


def test_dispute_median_overturns_with_two_low_passes(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)
    env.resolve_passes(bob, [100, 120, 900])  # median 120
    assert env.c.get_score(bob)["score"] == 120
    assert env.c.get_claimable(carol) == 2 * BOND
    env.assert_zero_wei([alice])


def test_resolve_requires_disputed(env, alice, bob):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 900)
    env.mock_score(900)
    env.vm.sender = bob
    with env.vm.expect_revert("not DISPUTED"):
        env.c.resolve_dispute(bob)


def test_llm_failure_in_dispute_reverts_and_does_not_slash(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)
    env.vm.clear_mocks()
    env.vm.mock_web(r".*", {"status": 200, "body": "data"})
    env.vm.mock_llm(r".*Solvency Score.*", json.dumps(json.dumps({"nonsense": 1})))
    env.vm.sender = env.rando
    with env.vm.expect_revert("LLM_ERROR"):
        env.c.resolve_dispute(bob)
    assert env.c.get_score(bob)["state"] == "DISPUTED"
    assert env.c.get_stake(alice) == 200 * GEN
    env.assert_zero_wei([alice])


# ======================================================================
# 404 evidence trap  [fix 6]
# ======================================================================

@pytest.mark.parametrize("status,body", [(404, "nope"), (403, "x"), (500, "x"), (200, "")])
def test_missing_evidence_slashes_evaluator(env, alice, bob, carol, status, body):
    stake = 200 * GEN
    env.stake(alice, stake)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)

    env.mock_web_status(status, body)  # evaluator pulled / broke the URL
    env.vm.sender = env.rando
    env.c.resolve_dispute(bob)         # must NOT revert / get stuck

    s = env.c.get_score(bob)
    assert s["state"] == "FINAL" and s["score"] == 0
    assert s["exposure"] == 0 and env.c.get_exposure(alice) == 0
    assert env.c.get_stake(alice) == stake - MIN_STAKE // 2 - BOND
    assert env.c.get_treasury() == MIN_STAKE // 2
    assert env.c.get_claimable(carol) == 2 * BOND
    assert env.c.get_disputed_count(alice) == 0
    assert env.c.get_max_borrow_power(bob) == 0
    env.assert_zero_wei([alice])
    env.withdraw(carol)
    env.assert_zero_wei([alice])


def test_unreachable_evidence_slashes_evaluator(env, alice, bob, carol):
    """No web response at all (network failure) is also the evaluator's fault."""
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)
    env.vm.clear_mocks()  # nothing mocked -> fetch raises
    env.vm.sender = env.rando
    env.c.resolve_dispute(bob)
    assert env.c.get_score(bob)["score"] == 0
    assert env.c.get_claimable(carol) == 2 * BOND
    env.assert_zero_wei([alice])


def test_missing_evidence_with_unbonded_stake_still_slashes(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 100)                 # exposure 20
    env.vm.sender = alice
    env.c.initiate_unstake(180 * GEN)            # PENDING: allowed; leaves exactly the exposure
    env.challenge(carol, bob)
    env.mock_web_status(404)
    env.vm.sender = env.rando
    env.c.resolve_dispute(bob)
    assert env.c.get_stake(alice) == 0           # slash 50 = 20 active + 30 unbonding
    assert env.c.get_unbonding(alice)["amount"] == 180 * GEN - 30 * GEN - BOND
    env.assert_zero_wei([alice])


# ======================================================================
# access control & payout  [fix 1 & 2]
# ======================================================================

def test_random_user_cannot_report_default(env, alice, bob, carol, dave):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    for attacker in (env.rando, carol, dave, bob, alice):
        env.vm.sender = attacker
        with env.vm.expect_revert("Only whitelisted lender"):
            env.c.report_default(bob, 1)
    assert env.c.get_stake(alice) == 200 * GEN
    assert env.c.get_score(bob)["score"] == 800
    assert env.transfers == []
    env.assert_zero_wei([alice])


def test_whitelist_is_governor_only(env, carol):
    env.vm.sender = carol
    with env.vm.expect_revert("Only governor"):
        env.c.whitelist_lender(carol)
    with env.vm.expect_revert("Only governor"):
        env.c.remove_lender(env.lender)
    assert not env.c.is_lender(carol)
    assert env.c.is_lender(env.lender)


def test_removed_lender_loses_access(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    env.vm.sender = env.governor
    env.c.remove_lender(env.lender)
    with env.vm.expect_revert("Only whitelisted lender"):
        env.default(bob, 1)


def test_distribute_treasury_removed(env):
    assert not hasattr(env.c, "distribute_treasury")
    with pytest.raises(AttributeError):
        env.c.distribute_treasury(env.lender, 1)


def test_governor_cannot_touch_treasury(env, alice, bob, carol):
    """Only path out of the treasury was removed: forfeited bonds stay put."""
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)
    env.resolve(bob, 880)  # upheld -> bond to treasury
    assert env.c.get_treasury() == BOND
    for name in ("distribute_treasury", "withdraw_treasury", "drain_treasury"):
        assert not hasattr(env.c, name)
    env.vm.sender = env.governor
    with env.vm.expect_revert("Nothing to withdraw"):
        env.c.withdraw()
    assert env.c.get_treasury() == BOND
    env.assert_zero_wei([alice])


@pytest.mark.parametrize("loss", [1, 37 * GEN, 160 * GEN, 10_000 * GEN])
def test_default_pays_lender_directly_capped_by_borrow_power(env, alice, bob, loss):
    stake = 200 * GEN
    env.stake(alice, stake)
    _finalize(env, alice, bob, 800)        # exposure = 160 GEN
    assert env.c.get_max_borrow_power(bob) == 160 * GEN
    env.default(bob, loss)
    paid = min(loss, 160 * GEN)
    assert env.transfers == [(env.lender, paid)]    # direct transfer, no claim step
    assert env.c.get_stake(alice) == stake - paid
    assert env.c.get_treasury() == 0                # slashes go to the lender, not treasury
    assert env.c.get_claimable(env.lender) == 0
    s = env.c.get_score(bob)
    assert s["score"] == 0 and s["exposure"] == 0
    assert env.c.get_exposure(alice) == 0
    env.assert_zero_wei([alice])
    with env.vm.expect_revert("Default already recorded"):
        env.default(bob, 1)


def test_default_rejections(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 800)
    with env.vm.expect_revert("not FINAL"):         # PENDING
        env.default(bob, 10)
    env.challenge(carol, bob)
    with env.vm.expect_revert("not FINAL"):         # DISPUTED
        env.default(bob, 10)
    with env.vm.expect_revert("No score for borrower"):
        env.default(carol, 10)
    env.resolve(bob, 800)
    with env.vm.expect_revert("Loss must be positive"):
        env.default(bob, 0)
    env.assert_zero_wei([alice])


def test_lender_cannot_be_evaluator_or_borrower(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    env.vm.sender = env.governor
    env.c.whitelist_lender(alice)
    env.c.whitelist_lender(bob)
    with env.vm.expect_revert("cannot be evaluator or borrower"):
        env.default(bob, 10, lender=alice)
    with env.vm.expect_revert("cannot be evaluator or borrower"):
        env.default(bob, 10, lender=bob)
    assert env.c.get_stake(alice) == 200 * GEN
    env.assert_zero_wei([alice])


def test_default_after_expiry_reverts(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    env.warp(TTL + EPS)
    with env.vm.expect_revert("Score expired"):
        env.default(bob, 10)
    env.assert_zero_wei([alice])


def test_default_slash_eats_unbonding(env, alice, bob, dave, carol):
    """A dispute slash can leave active stake below exposure; the default then
    reaches into the unbonding queue."""
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 500)                 # exposure 100
    env.request(alice, dave, 100)                # exposure 20
    env.vm.sender = alice
    env.c.initiate_unstake(80 * GEN)             # leaves 120 == exposure 120
    env.challenge(carol, dave)
    env.resolve(dave, 700)                       # overturned: slash 50 + reward 5 from active
    assert env.c.get_stake(alice) == 65 * GEN
    assert env.c.get_exposure(alice) == 120 * GEN    # commitment is NOT shrunk by the slash
    env.advance(timedelta(hours=25))
    env.finalize(bob)
    env.default(bob, 10_000 * GEN)               # claim 100: 65 active + 35 unbonding
    assert env.transfers == [(env.lender, 100 * GEN)]
    assert env.c.get_stake(alice) == 0
    assert env.c.get_unbonding(alice)["amount"] == 45 * GEN
    env.assert_zero_wei([alice])
    env.warp(timedelta(days=9))
    env.vm.sender = alice
    assert env.c.claim_unstaked() == 45 * GEN    # only what survived
    env.assert_zero_wei([alice])


def test_default_within_active_leaves_unbonding_intact(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)              # exposure 160
    env.vm.sender = alice
    env.c.initiate_unstake(40 * GEN)
    env.default(bob, 30 * GEN)
    assert env.c.get_stake(alice) == 130 * GEN
    assert env.c.get_unbonding(alice)["amount"] == 40 * GEN
    env.assert_zero_wei([alice])


def test_default_capped_by_exposure(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)              # exposure 160
    env.vm.sender = alice
    env.c.initiate_unstake(40 * GEN)             # free stake only
    env.default(bob, 10_000 * GEN)               # claim capped at exposure 160
    assert env.c.get_stake(alice) == 0
    assert env.c.get_unbonding(alice)["amount"] == 40 * GEN
    assert env.transfers == [(env.lender, 160 * GEN)]
    env.assert_zero_wei([alice])


def test_default_payout_capped_by_what_is_left(env, alice, bob, dave, carol):
    """A dispute slash leaves less stake than the exposure: the lender gets what exists."""
    env.stake(alice, MIN_STAKE)
    env.request(alice, bob, 500)                 # exposure 50
    env.request(alice, dave, 100)                # exposure 10
    env.challenge(carol, dave)
    env.resolve(dave, 700)                       # slash 50 + reward 5 -> active 45
    assert env.c.get_stake(alice) == 45 * GEN
    env.advance(timedelta(hours=25))
    env.finalize(bob)
    env.default(bob, 10_000 * GEN)
    assert env.transfers == [(env.lender, 45 * GEN)]   # < exposure 50
    assert env.c.get_stake(alice) == 0
    env.assert_zero_wei([alice])


# ======================================================================
# cumulative exposure  [fix 3]
# ======================================================================

def test_exposure_cannot_exceed_stake(env, alice):
    b = env.many
    env.stake(alice, MIN_STAKE)                 # 100 GEN
    env.request(alice, b[0], 600)               # 60
    assert env.c.get_exposure(alice) == 60 * GEN
    env.mock_score(500)
    env.vm.sender = alice
    with env.vm.expect_revert("Exposure exceeds stake"):  # 60 + 50 = 110 > 100
        env.c.request_score_update(b[1], URL)
    assert env.c.get_exposure(alice) == 60 * GEN
    env.request(alice, b[1], 400)               # 60 + 40 = 100 == stake: allowed
    assert env.c.get_exposure(alice) == 100 * GEN
    env.mock_score(1)
    env.vm.sender = alice
    with env.vm.expect_revert("Exposure exceeds stake"):  # 1/1000 of 100 GEN > 0 left
        env.c.request_score_update(b[2], URL)
    env.assert_exposure_invariant([alice], b)
    env.assert_zero_wei([alice])


def test_five_borrowers_cannot_leverage_100_gen_into_500(env, alice):
    b = env.many
    env.stake(alice, MIN_STAKE)
    ok = 0
    for borrower in b[:5]:
        env.mock_score(300)
        env.vm.sender = alice
        try:
            env.c.request_score_update(borrower, URL)
            ok += 1
        except Exception as ex:
            assert "Exposure exceeds stake" in str(ex)
    assert ok == 3  # 3 x 30 GEN = 90; the 4th would reach 120 > 100
    total_power = 0
    for borrower in b[:5]:
        try:
            total_power += env.c.get_score(borrower)["exposure"]
        except Exception:
            pass
    assert total_power <= 100 * GEN
    env.assert_exposure_invariant([alice], b)


def test_exposure_is_per_evaluator(env, alice, bob):
    b = env.many
    env.stake(alice, MIN_STAKE)
    env.stake(bob, MIN_STAKE)
    env.request(alice, b[0], 1000)   # alice fully utilised (100)
    env.request(bob, b[1], 1000)     # bob independent
    assert env.c.get_exposure(alice) == 100 * GEN
    assert env.c.get_exposure(bob) == 100 * GEN
    env.assert_exposure_invariant([alice, bob], b)


def test_replaced_after_expiry_frees_old_exposure(env, alice, bob):
    env.stake(alice, MIN_STAKE)
    _finalize(env, alice, bob, 1000)             # full 100
    assert env.c.get_exposure(alice) == 100 * GEN
    env.warp(TTL + timedelta(hours=1))
    env.request(alice, bob, 300)                 # expired: replace, 100 freed, 30 taken
    assert env.c.get_exposure(alice) == 30 * GEN
    assert env.c.get_score(bob)["state"] == "PENDING"
    env.assert_exposure_invariant([alice], [bob])


def test_expired_exposure_can_be_released_and_reused(env, alice):
    b = env.many
    env.stake(alice, MIN_STAKE)
    _finalize(env, alice, b[0], 1000)            # uses everything
    env.mock_score(500)
    env.vm.sender = alice
    with env.vm.expect_revert("Exposure exceeds stake"):
        env.c.request_score_update(b[1], URL)

    env.vm.sender = env.rando
    with env.vm.expect_revert("has not expired"):
        env.c.release_expired(b[0])
    env.warp(TTL)                                 # exactly 30d: not yet expired
    with env.vm.expect_revert("has not expired"):
        env.c.release_expired(b[0])
    env.warp(TTL + timedelta(hours=1))
    env.c.release_expired(b[0])
    assert env.c.get_exposure(alice) == 0
    env.c.release_expired(b[0])                   # idempotent
    env.request(alice, b[1], 500)
    assert env.c.get_exposure(alice) == 50 * GEN
    env.assert_exposure_invariant([alice], b)
    env.assert_zero_wei([alice])


def test_release_expired_rejects_disputed(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 800)
    env.challenge(carol, bob)
    env.warp(TTL + timedelta(days=1))
    env.vm.sender = env.rando
    with env.vm.expect_revert("Score is disputed"):
        env.c.release_expired(bob)
    assert env.c.get_exposure(alice) == 160 * GEN


def test_overturn_does_not_grow_exposure(env, alice, bob, carol):
    env.stake(alice, 300 * GEN)
    env.request(alice, bob, 400)                  # exposure 120
    env.challenge(carol, bob)
    env.resolve(bob, 900)                         # overturned *upward*
    s = env.c.get_score(bob)
    assert s["score"] == 900
    assert s["exposure"] <= 120 * GEN             # never grows past the checked commitment
    assert env.c.get_exposure(alice) == s["exposure"]
    env.assert_zero_wei([alice])


def test_default_frees_exposure(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 500)
    assert env.c.get_exposure(alice) == 100 * GEN
    env.default(bob, 10 * GEN)
    assert env.c.get_exposure(alice) == 0
    env.assert_exposure_invariant([alice], [bob])


# ======================================================================
# premature borrowing & expiration  [fix 4]
# ======================================================================

def test_pending_score_has_zero_borrow_power(env, alice, bob):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 800)
    assert env.c.get_score(bob)["state"] == "PENDING"
    assert env.c.get_max_borrow_power(bob) == 0
    env.warp(timedelta(hours=24))                 # window over but not finalized
    assert env.c.get_max_borrow_power(bob) == 0
    env.finalize(bob)
    assert env.c.get_max_borrow_power(bob) == 160 * GEN


def test_disputed_score_has_zero_borrow_power(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 800)
    env.challenge(carol, bob)
    assert env.c.get_max_borrow_power(bob) == 0
    env.resolve(bob, 800)
    assert env.c.get_max_borrow_power(bob) == 160 * GEN


def test_borrow_power_expires_after_30_days(env, alice, bob):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 800)                  # issued at T0
    env.warp(timedelta(hours=25))
    env.finalize(bob)
    env.warp(TTL)                                 # exactly timestamp + 30d: still valid
    assert env.c.get_max_borrow_power(bob) == 160 * GEN
    env.warp(TTL + EPS)                           # one second later: expired
    assert env.c.get_max_borrow_power(bob) == 0


def test_borrow_power_math_and_dynamic_drop(env, alice, bob):
    assert env.c.get_max_borrow_power(bob) == 0
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 400)               # exposure 80
    assert env.c.get_max_borrow_power(bob) == (400 * 200 * GEN) // 1000
    env.vm.sender = alice
    env.c.initiate_unstake(100 * GEN)             # stake 100 >= exposure 80: allowed
    assert env.c.get_max_borrow_power(bob) == (400 * 100 * GEN) // 1000   # 40 < committed 80


def test_topup_cannot_inflate_borrow_power(env, alice, bob):
    env.stake(alice, MIN_STAKE)
    _finalize(env, alice, bob, 500)               # committed 50
    env.stake(alice, 900 * GEN)                   # stake 1000: live power would be 500
    assert env.c.get_max_borrow_power(bob) == 50 * GEN


def test_borrow_power_floor_division(env, alice, bob):
    env.stake(alice, MIN_STAKE + 1)
    _finalize(env, alice, bob, 333)
    assert env.c.get_max_borrow_power(bob) == (333 * (MIN_STAKE + 1)) // 1000


# ======================================================================
# state overwrite  [fix 7]
# ======================================================================

def test_other_evaluator_cannot_overwrite_active_score(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.stake(bob, 200 * GEN)
    _finalize(env, alice, carol, 800)             # FINAL, within 30d
    env.mock_score(100)
    env.vm.sender = bob
    with env.vm.expect_revert("belongs to another evaluator"):
        env.c.request_score_update(carol, URL)
    s = env.c.get_score(carol)
    assert s["evaluator"] == alice.as_hex and s["score"] == 800
    assert env.c.get_exposure(alice) == 160 * GEN
    assert env.c.get_exposure(bob) == 0
    env.assert_zero_wei([alice, bob])


def test_cannot_overwrite_pending_or_disputed(env, alice, bob, carol, dave):
    env.stake(alice, 200 * GEN)
    env.stake(bob, 200 * GEN)
    env.request(alice, carol, 800)                # PENDING
    env.mock_score(100)
    for who in (bob, alice):                      # not even the owner mid-challenge
        env.vm.sender = who
        with env.vm.expect_revert("still in challenge process"):
            env.c.request_score_update(carol, URL)
    env.challenge(dave, carol)                    # DISPUTED
    for who in (bob, alice):
        env.vm.sender = who
        with env.vm.expect_revert("Score is disputed"):
            env.c.request_score_update(carol, URL)
    assert env.c.get_score(carol)["evaluator"] == alice.as_hex
    env.assert_zero_wei([alice, bob])


def test_overwrite_allowed_after_expiry_and_frees_old_exposure(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.stake(bob, 200 * GEN)
    _finalize(env, alice, carol, 800)
    env.warp(TTL + timedelta(hours=1))
    env.request(bob, carol, 600)
    s = env.c.get_score(carol)
    assert s["evaluator"] == bob.as_hex and s["state"] == "PENDING"
    assert env.c.get_exposure(alice) == 0         # old commitment released
    assert env.c.get_exposure(bob) == 120 * GEN
    env.assert_exposure_invariant([alice, bob], [carol])
    env.assert_zero_wei([alice, bob])


def test_same_evaluator_can_rescore_once_liability_is_settled(env, alice, carol):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, carol, 800)
    env.default(carol, 10 * GEN)                 # liability settled: score 0, exposure 0
    env.request(alice, carol, 700)
    assert env.c.get_score(carol)["score"] == 700
    assert env.c.get_exposure(alice) == 700 * 190 * GEN // 1000   # stake is 190 after the slash


# ======================================================================
# front-running gaps: score evasion & exposure evasion
# ======================================================================

def test_active_score_cannot_be_lowered_to_zero_before_expiry(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)              # lenders may now lend against 160 GEN
    for new_score in (0, 100, 900):              # lowering OR raising: both blocked
        env.mock_score(new_score)
        env.vm.sender = alice
        with env.vm.expect_revert("cannot be replaced before expiry"):
            env.c.request_score_update(bob, URL)
    s = env.c.get_score(bob)
    assert s["score"] == 800 and s["state"] == "FINAL" and s["exposure"] == 160 * GEN
    assert env.c.get_exposure(alice) == 160 * GEN
    # liability intact: a lender can still collect
    env.default(bob, 10_000 * GEN)
    assert env.transfers == [(env.lender, 160 * GEN)]
    env.assert_zero_wei([alice])


def test_score_replaceable_exactly_after_expiry(env, alice, bob):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 800)
    env.advance(timedelta(hours=25))
    env.finalize(bob)
    env.warp(TTL)                                 # timestamp + 30d: still live
    env.mock_score(0)
    env.vm.sender = alice
    with env.vm.expect_revert("cannot be replaced before expiry"):
        env.c.request_score_update(bob, URL)
    env.warp(TTL + EPS)
    env.c.request_score_update(bob, URL)          # expired: allowed
    assert env.c.get_score(bob)["score"] == 0
    assert env.c.get_exposure(alice) == 0


def test_score_without_liability_can_be_replaced(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 0)                 # exposure 0: nothing to evade
    env.request(alice, bob, 500)
    assert env.c.get_score(bob)["score"] == 500


def test_cannot_unstake_below_exposure(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)              # exposure 160 -> 40 GEN free
    env.vm.sender = alice
    with env.vm.expect_revert("below exposure"):
        env.c.initiate_unstake(40 * GEN + 1)
    with env.vm.expect_revert("below exposure"):
        env.c.initiate_unstake(200 * GEN)
    assert env.c.get_stake(alice) == 200 * GEN
    assert env.c.get_unbonding(alice)["amount"] == 0
    env.c.initiate_unstake(40 * GEN)             # exactly the free stake
    assert env.c.get_stake(alice) == 160 * GEN
    with env.vm.expect_revert("below exposure"):
        env.c.initiate_unstake(1)
    env.assert_zero_wei([alice])


def test_unstake_guard_counts_pending_and_all_scores(env, alice):
    b = env.many
    env.stake(alice, 300 * GEN)
    env.request(alice, b[0], 400)                # PENDING exposure 120
    env.request(alice, b[1], 300)                # PENDING exposure 90
    env.vm.sender = alice
    with env.vm.expect_revert("below exposure"):
        env.c.initiate_unstake(91 * GEN)         # 300 - 91 = 209 < 210
    env.c.initiate_unstake(90 * GEN)
    env.assert_exposure_invariant([alice], b)
    env.assert_zero_wei([alice])


def test_unstake_unlocks_after_exposure_is_released(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    env.vm.sender = alice
    with env.vm.expect_revert("below exposure"):
        env.c.initiate_unstake(200 * GEN)
    env.warp(TTL + timedelta(hours=1))
    env.vm.sender = env.rando
    env.c.release_expired(bob)
    env.vm.sender = alice
    env.c.initiate_unstake(200 * GEN)            # nothing backs live scores any more
    assert env.c.get_stake(alice) == 0
    env.assert_zero_wei([alice])


def test_unstake_unlocks_after_default_settles_liability(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    env.default(bob, 50 * GEN)                   # stake 150, exposure 0
    env.vm.sender = alice
    env.c.initiate_unstake(150 * GEN)
    assert env.c.get_stake(alice) == 0
    env.assert_zero_wei([alice])


# ======================================================================
# unstaking / unbonding
# ======================================================================

def _unstake(env, who, amount):
    env.vm.sender = who
    env.c.initiate_unstake(amount)


def _claim_unstaked(env, who):
    env.vm.sender = who
    return env.c.claim_unstaked()


def test_unstake_flow(env, alice, bob):
    env.stake(alice, 300 * GEN)
    _finalize(env, alice, bob, 800)               # exposure 240; clock T0 + 25h
    assert env.c.get_max_borrow_power(bob) == 800 * 300 * GEN // 1000
    _unstake(env, alice, 60 * GEN)                # only the free 60 may leave
    assert env.c.get_stake(alice) == 240 * GEN
    u = env.c.get_unbonding(alice)
    assert u["amount"] == 60 * GEN
    assert u["unlock_time"] == int((T0 + timedelta(hours=25) + WEEK).timestamp())
    assert env.c.get_claimable(alice) == 0
    assert env.c.get_max_borrow_power(bob) == 800 * 240 * GEN // 1000   # live stake drops power
    env.assert_zero_wei([alice])

    env.vm.warp(iso(T0 + timedelta(hours=25) + WEEK - EPS))
    with env.vm.expect_revert("Still unbonding"):
        _claim_unstaked(env, alice)
    env.vm.warp(iso(T0 + timedelta(hours=25) + WEEK))
    assert _claim_unstaked(env, alice) == 60 * GEN
    assert env.c.get_claimable(alice) == 60 * GEN
    env.assert_zero_wei([alice])
    assert env.withdraw(alice) == 60 * GEN
    env.assert_zero_wei([alice])
    with env.vm.expect_revert("Nothing unbonding"):
        _claim_unstaked(env, alice)


def test_claim_before_seven_days_reverts(env, alice):
    env.stake(alice, 200 * GEN)
    _unstake(env, alice, 50 * GEN)
    for delta in (timedelta(0), timedelta(days=1), timedelta(days=6, hours=23, minutes=59)):
        env.warp(delta)
        with env.vm.expect_revert("Still unbonding"):
            _claim_unstaked(env, alice)
    assert env.c.get_unbonding(alice)["amount"] == 50 * GEN
    env.assert_zero_wei([alice])


def test_unstake_rejections(env, alice, bob):
    env.stake(alice, 150 * GEN)
    env.vm.sender = alice
    with env.vm.expect_revert("Amount must be positive"):
        env.c.initiate_unstake(0)
    with env.vm.expect_revert("Amount exceeds stake"):
        env.c.initiate_unstake(150 * GEN + 1)
    env.vm.sender = bob
    with env.vm.expect_revert("Amount exceeds stake"):
        env.c.initiate_unstake(1)
    with env.vm.expect_revert("Nothing unbonding"):
        env.c.claim_unstaked()
    env.assert_zero_wei([alice])


def test_second_unstake_accumulates_and_restarts_timer(env, alice):
    env.stake(alice, 200 * GEN)
    _unstake(env, alice, 30 * GEN)
    env.warp(timedelta(days=3))
    _unstake(env, alice, 20 * GEN)
    u = env.c.get_unbonding(alice)
    assert u["amount"] == 50 * GEN
    assert u["unlock_time"] == int((T0 + timedelta(days=10)).timestamp())
    env.warp(timedelta(days=8))
    with env.vm.expect_revert("Still unbonding"):
        _claim_unstaked(env, alice)
    env.warp(timedelta(days=10))
    assert _claim_unstaked(env, alice) == 50 * GEN
    env.assert_zero_wei([alice])


def test_below_min_stake_blocks_new_scores_only(env, alice, bob, carol):
    env.stake(alice, 150 * GEN)
    _finalize(env, alice, bob, 300)               # exposure 45
    _unstake(env, alice, 51 * GEN)                # active 99 < min_stake, still >= exposure
    env.mock_score(500)
    env.vm.sender = alice
    with env.vm.expect_revert("not a staked evaluator"):
        env.c.request_score_update(carol, URL)
    assert env.c.get_score(bob)["score"] == 300
    assert env.c.get_max_borrow_power(bob) == 300 * 99 * GEN // 1000
    env.assert_zero_wei([alice])


def test_dispute_slash_eats_unbonding(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 100)                  # exposure 20
    _unstake(env, alice, 180 * GEN)               # PENDING: allowed; active 20, unbonding 180
    env.challenge(carol, bob)
    env.resolve(bob, 700)                         # overturned
    left = 180 * GEN - 30 * GEN - BOND            # slash 50 = 20 active + 30 unbonding; reward 5
    assert env.c.get_stake(alice) == 0
    assert env.c.get_unbonding(alice)["amount"] == left
    assert env.c.get_treasury() == MIN_STAKE // 2
    assert env.c.get_claimable(carol) == 2 * BOND
    env.assert_zero_wei([alice])
    env.warp(timedelta(days=9))
    assert _claim_unstaked(env, alice) == left
    env.withdraw(alice)
    env.withdraw(carol)
    env.assert_zero_wei([alice])
    assert env.c.get_accounting()["total_claimable"] == 0


def test_dispute_lock_blocks_unstake_and_claim(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    _unstake(env, alice, 20 * GEN)
    env.request(alice, bob, 400)
    assert env.c.get_disputed_count(alice) == 0
    _unstake(env, alice, 5 * GEN)                 # PENDING does not lock
    env.challenge(carol, bob)
    assert env.c.get_disputed_count(alice) == 1
    with env.vm.expect_revert("disputed scores"):
        _unstake(env, alice, 1)
    env.warp(timedelta(days=30))
    with env.vm.expect_revert("disputed scores"):
        _claim_unstaked(env, alice)
    env.assert_zero_wei([alice])
    env.resolve(bob, 380)                         # upheld -> lock released
    assert env.c.get_disputed_count(alice) == 0
    assert _claim_unstaked(env, alice) == 25 * GEN
    _unstake(env, alice, 1 * GEN)
    env.assert_zero_wei([alice])


def test_dispute_lock_counts_multiple_scores(env, alice, bob, carol, dave):
    env.stake(alice, 300 * GEN)
    env.request(alice, bob, 400)
    env.request(alice, dave, 300)
    env.challenge(carol, bob)
    env.challenge(carol, dave)
    assert env.c.get_disputed_count(alice) == 2
    env.resolve(bob, 400)
    assert env.c.get_disputed_count(alice) == 1
    with env.vm.expect_revert("disputed scores"):
        _unstake(env, alice, 1)
    env.resolve(dave, 300)
    assert env.c.get_disputed_count(alice) == 0
    _unstake(env, alice, 1)
    env.assert_zero_wei([alice])


# ======================================================================
# end-to-end zero-wei
# ======================================================================

def test_full_lifecycle_zero_wei(env, alice, bob, carol, dave):
    b = env.many
    env.stake(alice, 300 * GEN)
    env.stake(bob, 123 * GEN + 7)
    env.request(alice, carol, 900)                # 270
    env.request(bob, dave, 400)
    env.assert_zero_wei([alice, bob])

    env.challenge(bob, carol)                     # overturned
    env.challenge(alice, dave)                    # upheld
    env.assert_zero_wei([alice, bob])
    env.resolve(carol, 500)
    env.resolve(dave, 450)
    env.assert_zero_wei([alice, bob])
    env.assert_exposure_invariant([alice, bob], [carol, dave])

    env.advance(timedelta(hours=1))
    env.default(carol, 1000 * GEN)                # capped by committed exposure
    env.default(dave, 5)
    env.assert_zero_wei([alice, bob])
    env.assert_exposure_invariant([alice, bob], [carol, dave])
    assert env.c.get_exposure(alice) == 0 and env.c.get_exposure(bob) == 0

    for who in (alice, bob):
        if env.c.get_claimable(who):
            env.withdraw(who)
    env.assert_zero_wei([alice, bob])
    assert env.c.get_accounting()["total_claimable"] == 0


def test_everything_zero_wei(env, alice, bob, carol, dave):
    env.stake(alice, 300 * GEN)
    env.stake(bob, 120 * GEN)
    env.request(alice, carol, 600)                # exposure 180
    _unstake(env, alice, 120 * GEN)               # leaves 180 == exposure; still slashable
    env.challenge(bob, carol)
    env.assert_zero_wei([alice, bob])
    env.resolve(carol, 300)                       # overturned
    env.request(bob, dave, 100)                   # exposure 12
    _unstake(env, bob, 100 * GEN)
    env.challenge(alice, dave)
    env.resolve(dave, 320)                        # upheld (diff 220 <= 250)
    env.assert_zero_wei([alice, bob])
    env.warp(timedelta(days=2))
    assert env.c.get_score(dave)["state"] == "FINAL"
    env.default(dave, 33 * GEN + 1)
    env.assert_zero_wei([alice, bob])
    env.warp(timedelta(days=30))
    for who in (alice, bob):
        if env.c.get_unbonding(who)["amount"]:
            _claim_unstaked(env, who)
        env.assert_zero_wei([alice, bob])
    for who in (alice, bob, carol):
        if env.c.get_claimable(who):
            env.withdraw(who)
        env.assert_zero_wei([alice, bob])
    assert env.c.get_accounting()["total_claimable"] == 0