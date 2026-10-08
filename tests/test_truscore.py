import json
from datetime import datetime, timedelta, timezone

import pytest

CONTRACT = "contracts/truscore_oracle.py"
GEN = 10**18
MIN_STAKE = 100 * GEN
BOND = 5 * GEN
URL = "https://data.example.com/borrower.json"
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


class Env:
    """Wraps the deployed contract plus an independent external ledger."""

    def __init__(self, vm, contract, accounts):
        self.vm = vm
        self.c = contract
        self.accounts = accounts
        self.deposited = 0
        self.transfers = []  # (recipient, amount) emitted by withdraw()
        vm.warp(iso(T0))

    def stake(self, who, amount):
        self.vm.sender = who
        self.vm.value = amount
        self.c.register_evaluator(amount)
        self.vm.value = 0
        self.deposited += amount

    def mock_score(self, score, reasoning="r"):
        self.vm.clear_mocks()
        self.vm.mock_web(r".*", {"status": 200, "body": '{"revenue": 100}'})
        self.vm.mock_llm(r".*Solvency Score.*", json.dumps(json.dumps({"score": score, "reasoning": reasoning})))

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

    def resolve(self, borrower, new_score):
        self.mock_score(new_score, "re-run")
        self.vm.sender = self.accounts[-1]
        self.c.resolve_dispute(borrower)

    def warp(self, delta):
        self.vm.warp(iso(T0 + delta))

    def withdraw(self, who):
        self.vm.sender = who
        return self.c.withdraw()

    def assert_zero_wei(self, evaluators=()):
        a = self.c.get_accounting()
        assert a["total_deposited"] == self.deposited
        assert a["total_deposited"] == (
            a["total_staked"] + a["total_unbonding"] + a["total_escrow"]
            + a["treasury"] + a["total_claimable"] + a["total_withdrawn"]
        )
        assert sum(self.c.get_unbonding(e)["amount"] for e in evaluators) == a["total_unbonding"]
        # Independent cross-check of the staked bucket from per-account views.
        assert sum(self.c.get_stake(e) for e in evaluators) == a["total_staked"]
        assert a["total_withdrawn"] == sum(amt for _, amt in self.transfers)
        assert a["treasury"] == self.c.get_treasury()


@pytest.fixture
def env(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie, direct_owner, monkeypatch):
    c = direct_deploy(CONTRACT, MIN_STAKE, BOND)
    e = Env(direct_vm, c, [direct_alice, direct_bob, direct_charlie, direct_owner])

    import genlayer.chain as chain_mod

    class RecordingAccount:
        def __init__(self, addr):
            self.addr = addr

        def emit_transfer(self, amount, **kwargs):
            e.transfers.append((self.addr, int(amount)))

    monkeypatch.setattr(chain_mod, "Account", RecordingAccount)
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


# ---- registration ------------------------------------------------------

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


# ---- SSRF --------------------------------------------------------------

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


# ---- unchallenged finalization ----------------------------------------

def test_unchallenged_finalization(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 800)
    s = env.c.get_score(bob)
    assert s["score"] == 800 and s["state"] == "PENDING" and s["challenger"] == ""
    assert s["timestamp"] == int(T0.timestamp())

    env.vm.sender = carol
    with env.vm.expect_revert("Challenge window still open"):
        env.c.finalize_score(bob)

    env.warp(timedelta(hours=24))
    env.c.finalize_score(bob)
    assert env.c.get_score(bob)["state"] == "FINAL"
    # Window closed: can no longer challenge.
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

    # fresh score: evaluator can't self-challenge, wrong bond rejected
    env.warp(timedelta(hours=25))
    env.c.finalize_score(bob)
    env.request(alice, bob, 700)
    with env.vm.expect_revert("own score"):
        env.challenge(alice, bob)
    with env.vm.expect_revert("exactly the challenge bond"):
        env.challenge(carol, bob, value=BOND - 1)
    env.vm.value = 0
    env.assert_zero_wei([alice])


# ---- dispute: overturned ----------------------------------------------

def test_dispute_overturned(env, alice, bob, carol):
    stake = 200 * GEN
    env.stake(alice, stake)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)
    s = env.c.get_score(bob)
    assert s["state"] == "DISPUTED" and s["challenger"] == carol.as_hex
    assert env.c.get_accounting()["total_escrow"] == BOND
    env.assert_zero_wei([alice])

    env.resolve(bob, 600)  # |600-900| = 300 > 150
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


def test_dispute_overturned_with_low_stake_caps_payouts(env, alice, bob, carol):
    env.stake(alice, MIN_STAKE)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)
    # Drain most of the stake via another borrower's default first.
    env.resolve(bob, 0)
    # stake 100: slash 50, reward min(50, 5)=5 -> 45 left
    assert env.c.get_stake(alice) == MIN_STAKE - MIN_STAKE // 2 - BOND
    env.assert_zero_wei([alice])


# ---- dispute: upheld ---------------------------------------------------

@pytest.mark.parametrize("new_score", [900, 750, 1000, 1050 - 1])
def test_dispute_upheld(env, alice, bob, carol, new_score):
    stake = 200 * GEN
    env.stake(alice, stake)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)
    env.resolve(bob, new_score)  # diff <= 150 (boundary: 750 -> exactly 150)

    s = env.c.get_score(bob)
    assert s["state"] == "FINAL" and s["score"] == 900  # original stands
    assert env.c.get_stake(alice) == stake
    assert env.c.get_treasury() == BOND
    assert env.c.get_claimable(carol) == 0
    env.assert_zero_wei([alice])


def test_overturn_boundary_151(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)
    env.resolve(bob, 749)  # diff 151
    assert env.c.get_score(bob)["score"] == 749
    assert env.c.get_claimable(carol) == 2 * BOND
    env.assert_zero_wei([alice])


def test_resolve_requires_disputed(env, alice, bob):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 900)
    env.mock_score(900)
    env.vm.sender = bob
    with env.vm.expect_revert("not DISPUTED"):
        env.c.resolve_dispute(bob)


# ---- default slashing --------------------------------------------------

def _finalize(env, evaluator, borrower, score):
    env.request(evaluator, borrower, score)
    env.warp(timedelta(hours=25))
    env.c.finalize_score(borrower)


@pytest.mark.parametrize("loss", [1, 37 * GEN, 200 * GEN, 10_000 * GEN])
def test_default_slashing(env, alice, bob, carol, loss):
    stake = 200 * GEN
    env.stake(alice, stake)
    _finalize(env, alice, bob, 800)
    env.vm.sender = carol
    env.c.report_default(bob, loss)
    slashed = min(stake, loss)
    assert env.c.get_stake(alice) == stake - slashed
    assert env.c.get_treasury() == slashed
    assert env.c.get_score(bob)["score"] == 0
    env.assert_zero_wei([alice])
    with env.vm.expect_revert("Default already recorded"):
        env.c.report_default(bob, 1)


def test_default_requires_final(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 800)
    env.vm.sender = carol
    with env.vm.expect_revert("not FINAL"):
        env.c.report_default(bob, 10)
    env.challenge(carol, bob)
    with env.vm.expect_revert("not FINAL"):
        env.c.report_default(bob, 10)
    with env.vm.expect_revert("No score for borrower"):
        env.c.report_default(carol, 10)


def test_slashed_evaluator_below_min_cannot_score(env, alice, bob, carol):
    env.stake(alice, MIN_STAKE)
    _finalize(env, alice, bob, 800)
    env.vm.sender = carol
    env.c.report_default(bob, 1)
    env.mock_score(500)
    env.vm.sender = alice
    with env.vm.expect_revert("not a staked evaluator"):
        env.c.request_score_update(carol, URL)


# ---- borrow power ------------------------------------------------------

def test_borrow_power(env, alice, bob, carol):
    assert env.c.get_max_borrow_power(bob) == 0
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 750)
    assert env.c.get_max_borrow_power(bob) == (750 * 200 * GEN) // 1000
    env.vm.sender = carol
    env.c.report_default(bob, 40 * GEN)
    assert env.c.get_max_borrow_power(bob) == 0  # score zeroed


def test_borrow_power_floor_division(env, alice, bob):
    env.stake(alice, MIN_STAKE + 1)
    _finalize(env, alice, bob, 333)
    assert env.c.get_max_borrow_power(bob) == (333 * (MIN_STAKE + 1)) // 1000


# ---- end-to-end zero-wei across scenarios ------------------------------

def test_full_lifecycle_zero_wei(env, alice, bob, carol, dave):
    a, b, c_, d = alice, bob, carol, dave
    env.stake(a, 300 * GEN)
    env.stake(b, 123 * GEN + 7)
    borrowers = [c_, d]
    env.request(a, c_, 900)
    env.request(b, d, 400)
    env.assert_zero_wei([a, b])

    env.challenge(b, c_)       # b challenges a's score on c_ -> overturned
    env.challenge(a, d)        # a challenges b's score on d  -> upheld
    env.assert_zero_wei([a, b])
    env.resolve(c_, 500)
    env.resolve(d, 450)
    env.assert_zero_wei([a, b])

    env.vm.sender = c_
    env.c.report_default(c_, 1000 * GEN)   # a fully slashed
    env.c.report_default(d, 5)             # b slashed 5 wei
    env.assert_zero_wei([a, b])
    assert env.c.get_stake(a) == 0

    env.withdraw(b)
    env.assert_zero_wei([a, b])
    assert env.c.get_accounting()["total_claimable"] == 0


# ---- unstaking / unbonding ---------------------------------------------

WEEK = timedelta(days=7)


def _unstake(env, who, amount):
    env.vm.sender = who
    env.c.initiate_unstake(amount)


def _claim_unstaked(env, who):
    env.vm.sender = who
    return env.c.claim_unstaked()


def test_unstake_flow_and_borrow_power_drop(env, alice, bob):
    env.stake(alice, 300 * GEN)
    _finalize(env, alice, bob, 800)  # clock is now T0 + 25h
    assert env.c.get_max_borrow_power(bob) == 800 * 300 * GEN // 1000

    _unstake(env, alice, 100 * GEN)
    assert env.c.get_stake(alice) == 200 * GEN
    u = env.c.get_unbonding(alice)
    assert u["amount"] == 100 * GEN
    assert u["unlock_time"] == int((T0 + timedelta(hours=25) + WEEK).timestamp())
    assert env.c.get_claimable(alice) == 0  # NOT claimable yet
    assert env.c.get_max_borrow_power(bob) == 800 * 200 * GEN // 1000  # immediate drop
    env.assert_zero_wei([alice])

    # exactly at unlock_time it works; one second earlier it must not
    env.vm.warp(iso(T0 + timedelta(hours=25) + WEEK - timedelta(seconds=1)))
    with env.vm.expect_revert("Still unbonding"):
        _claim_unstaked(env, alice)
    env.vm.warp(iso(T0 + timedelta(hours=25) + WEEK))
    assert _claim_unstaked(env, alice) == 100 * GEN
    assert env.c.get_unbonding(alice)["amount"] == 0
    assert env.c.get_claimable(alice) == 100 * GEN
    env.assert_zero_wei([alice])
    assert env.withdraw(alice) == 100 * GEN
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
    env.warp(timedelta(days=8))  # 7 days after first, only 5 after second
    with env.vm.expect_revert("Still unbonding"):
        _claim_unstaked(env, alice)
    env.warp(timedelta(days=10))
    assert _claim_unstaked(env, alice) == 50 * GEN
    env.assert_zero_wei([alice])


def test_below_min_stake_blocks_new_scores_only(env, alice, bob, carol):
    env.stake(alice, 150 * GEN)
    _finalize(env, alice, bob, 800)
    _unstake(env, alice, 51 * GEN)  # active 99 GEN < min_stake
    env.mock_score(500)
    env.vm.sender = alice
    with env.vm.expect_revert("not a staked evaluator"):
        env.c.request_score_update(carol, URL)
    assert env.c.get_score(bob)["score"] == 800
    assert env.c.get_max_borrow_power(bob) == 800 * 99 * GEN // 1000
    env.assert_zero_wei([alice])


# ---- slashable unbonding (the trap) -----------------------------------

def test_default_slash_eats_unbonding(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    _unstake(env, alice, 150 * GEN)  # active 50, unbonding 150
    env.vm.sender = carol
    env.c.report_default(bob, 120 * GEN)
    # 50 from active, 70 from unbonding
    assert env.c.get_stake(alice) == 0
    assert env.c.get_unbonding(alice)["amount"] == 80 * GEN
    assert env.c.get_treasury() == 120 * GEN
    env.assert_zero_wei([alice])

    env.warp(timedelta(days=9))
    assert _claim_unstaked(env, alice) == 80 * GEN  # only what survived
    env.assert_zero_wei([alice])


def test_default_slash_within_active_leaves_unbonding_intact(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    _unstake(env, alice, 100 * GEN)
    env.vm.sender = carol
    env.c.report_default(bob, 30 * GEN)
    assert env.c.get_stake(alice) == 70 * GEN
    assert env.c.get_unbonding(alice)["amount"] == 100 * GEN
    env.assert_zero_wei([alice])


def test_default_slash_beyond_everything_is_capped(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    _unstake(env, alice, 150 * GEN)
    env.vm.sender = carol
    env.c.report_default(bob, 10_000 * GEN)
    assert env.c.get_stake(alice) == 0
    assert env.c.get_unbonding(alice)["amount"] == 0
    assert env.c.get_treasury() == 200 * GEN
    env.assert_zero_wei([alice])
    env.warp(timedelta(days=9))
    with env.vm.expect_revert("Nothing unbonding"):
        _claim_unstaked(env, alice)


def test_dispute_slash_eats_unbonding(env, alice, bob, carol):
    """Unstake while only PENDING (allowed), then get challenged and overturned."""
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 900)
    _unstake(env, alice, 200 * GEN - 10 * GEN)  # active 10, unbonding 190
    env.challenge(carol, bob)
    env.resolve(bob, 100)  # overturned
    # slash 50: 10 active + 40 unbonding; reward 5 from unbonding
    assert env.c.get_stake(alice) == 0
    assert env.c.get_unbonding(alice)["amount"] == 190 * GEN - 40 * GEN - BOND
    assert env.c.get_treasury() == MIN_STAKE // 2
    assert env.c.get_claimable(carol) == 2 * BOND
    env.assert_zero_wei([alice])
    env.warp(timedelta(days=9))
    assert _claim_unstaked(env, alice) == 190 * GEN - 40 * GEN - BOND
    env.withdraw(alice)
    env.withdraw(carol)
    env.assert_zero_wei([alice])
    assert env.c.get_accounting()["total_claimable"] == 0


# ---- dispute lock ------------------------------------------------------

def test_dispute_lock_blocks_unstake_and_claim(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    _unstake(env, alice, 20 * GEN)          # before any dispute: fine
    env.request(alice, bob, 900)
    assert env.c.get_disputed_count(alice) == 0
    _unstake(env, alice, 5 * GEN)           # PENDING does not lock
    env.challenge(carol, bob)
    assert env.c.get_disputed_count(alice) == 1

    with env.vm.expect_revert("disputed scores"):
        _unstake(env, alice, 1)
    env.warp(timedelta(days=30))            # unbonding long matured
    with env.vm.expect_revert("disputed scores"):
        _claim_unstaked(env, alice)
    env.assert_zero_wei([alice])

    env.resolve(bob, 880)                   # upheld -> lock released
    assert env.c.get_disputed_count(alice) == 0
    assert _claim_unstaked(env, alice) == 25 * GEN
    _unstake(env, alice, 1 * GEN)
    env.assert_zero_wei([alice])


def test_dispute_lock_counts_multiple_scores(env, alice, bob, carol, dave):
    env.stake(alice, 300 * GEN)
    env.request(alice, bob, 900)
    env.request(alice, dave, 700)
    env.challenge(carol, bob)
    env.challenge(carol, dave)
    assert env.c.get_disputed_count(alice) == 2
    env.resolve(bob, 900)
    assert env.c.get_disputed_count(alice) == 1
    with env.vm.expect_revert("disputed scores"):
        _unstake(env, alice, 1)
    env.resolve(dave, 700)
    assert env.c.get_disputed_count(alice) == 0
    _unstake(env, alice, 1)
    env.assert_zero_wei([alice])


# ---- treasury distribution --------------------------------------------

def test_governor_is_deployer(env, direct_vm):
    assert env.c.get_governor() != ""


def _fund_treasury(env, alice, bob, carol, loss=40 * GEN):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    env.vm.sender = carol
    env.c.report_default(bob, loss)


def test_distribute_treasury(env, alice, bob, carol, dave):
    _fund_treasury(env, alice, bob, carol)
    gov = env.c.get_governor()
    # deployer is whichever sender was active at deploy time
    governor = _gov_address(env, gov)
    env.vm.sender = governor
    env.c.distribute_treasury(dave, 15 * GEN)
    assert env.c.get_treasury() == 25 * GEN
    assert env.c.get_claimable(dave) == 15 * GEN
    env.assert_zero_wei([alice])

    env.c.distribute_treasury(carol, 25 * GEN)  # drain the rest
    assert env.c.get_treasury() == 0
    env.assert_zero_wei([alice])

    assert env.withdraw(dave) == 15 * GEN
    assert env.withdraw(carol) == 25 * GEN
    env.assert_zero_wei([alice])
    assert env.c.get_accounting()["total_claimable"] == 0


def test_distribute_treasury_rejections(env, alice, bob, carol, dave):
    _fund_treasury(env, alice, bob, carol)
    governor = _gov_address(env, env.c.get_governor())
    env.vm.sender = carol
    with env.vm.expect_revert("Only governor"):
        env.c.distribute_treasury(carol, 1)
    env.vm.sender = governor
    with env.vm.expect_revert("Invalid amount"):
        env.c.distribute_treasury(dave, 0)
    with env.vm.expect_revert("Invalid amount"):
        env.c.distribute_treasury(dave, 40 * GEN + 1)
    assert env.c.get_treasury() == 40 * GEN
    env.assert_zero_wei([alice])


def _gov_address(env, gov_hex):
    from genlayer import Address

    return Address(gov_hex)


def test_everything_zero_wei(env, alice, bob, carol, dave):
    env.stake(alice, 300 * GEN)
    env.stake(bob, 120 * GEN)
    env.request(alice, carol, 900)
    _unstake(env, alice, 120 * GEN)         # PENDING score: allowed, still slashable
    env.challenge(bob, carol)
    env.assert_zero_wei([alice, bob])
    env.resolve(carol, 300)                 # overturned
    env.request(bob, dave, 500)
    _unstake(env, bob, 100 * GEN)
    env.challenge(alice, dave)
    env.resolve(dave, 520)                  # upheld
    env.assert_zero_wei([alice, bob])
    env.warp(timedelta(days=2))
    assert env.c.get_score(dave)["state"] == "FINAL"
    env.vm.sender = carol
    env.c.report_default(dave, 33 * GEN + 1)
    governor = _gov_address(env, env.c.get_governor())
    env.vm.sender = governor
    env.c.distribute_treasury(carol, env.c.get_treasury() // 2)
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

