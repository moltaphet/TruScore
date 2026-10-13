import json
from datetime import datetime, timedelta, timezone

import pytest

CONTRACT = "contracts/truscore_oracle.py"
GEN = 10**18
MIN_STAKE = 100 * GEN
BOND = 5 * GEN
URL = "https://data.example.com/borrower.json"
COUNTER = "https://counter.example.com/evidence.json"
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
WEEK = timedelta(days=7)
TTL = timedelta(days=30)
LOAN_TTL = timedelta(days=365)
OVERTURN_TIER_GAP = 2
EPS = timedelta(seconds=1)
FEE = GEN // 100                      # default evaluator fee paid with record_loan
DEFAULT_WINDOW = timedelta(days=3)
CLOSURE_WINDOW = timedelta(days=7)
EVIDENCE = "https://evidence.example.com/repayment.json"
LENDER_EVIDENCE = "https://evidence.example.com/default-claim.json"
TIMELOCK = timedelta(days=3)

DATA_ERR = "[DATA_UNAVAILABLE] evidence not found"
TRANSIENT_ERR = "[TRANSIENT] evidence temporarily unavailable"


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def llm_json(score, reasoning="r"):
    return json.dumps(json.dumps({"score": score, "reasoning": reasoning}))


class Env:
    """Wraps the deployed contract plus an independent external ledger."""

    def __init__(self, vm, contract):
        self.vm = vm
        self.c = contract
        self.deposited = 0
        self.transfers = []  # (recipient, amount): withdraw(), default and treasury payouts
        self.loan_pairs = set()
        self.t = T0
        vm.warp(iso(T0))

    # -- clock ---------------------------------------------------------
    def warp(self, delta):  # absolute offset from T0
        self.t = T0 + delta
        self.vm.warp(iso(self.t))

    def advance(self, delta):  # relative to the current clock
        self.t = self.t + delta
        self.vm.warp(iso(self.t))

    # -- mocks ---------------------------------------------------------
    def mock_score(self, score, reasoning="r"):
        self.vm.clear_mocks()
        self.vm.mock_web(r".*", {"status": 200, "body": '{"revenue": 100}'})
        self.vm.mock_llm(r".*Solvency Score.*", llm_json(score, reasoning))

    def mock_web_status(self, status, body="x"):
        self.vm.clear_mocks()
        self.vm.mock_web(r".*", {"status": status, "body": body})
        self.vm.mock_llm(r".*Solvency Score.*", llm_json(500, "x"))

    # -- actions -------------------------------------------------------
    def stake(self, who, amount):
        self.vm.sender = who
        self.vm.value = amount
        try:
            self.c.register_evaluator(amount)
        finally:
            self.vm.value = 0
        self.deposited += amount

    def request(self, evaluator, borrower, score):
        self.mock_score(score)
        self.vm.sender = evaluator
        self.c.request_score_update(borrower, URL)

    def challenge(self, who, borrower, counter_url="", value=BOND):
        self.vm.sender = who
        self.vm.value = value
        try:
            self.c.challenge_score(borrower, counter_url)
        finally:
            self.vm.value = 0
        self.deposited += value

    def resolve(self, borrower, new_score, caller=None):
        self.mock_score(new_score, "re-run")
        self.vm.sender = caller if caller is not None else self.rando
        self.c.resolve_dispute(borrower)

    def timelocked_lender(self, lender):
        """Governor whitelists a lender through the 3-day timelock (clock restored)."""
        sender, t = self.vm.sender, self.t
        self.vm.sender = self.governor
        self.c.propose_whitelist_lender(lender)
        self.advance(TIMELOCK)
        self.c.execute_whitelist_lender(lender)
        self.t = t
        self.vm.warp(iso(t))
        self.vm.sender = sender

    def claim_treasury(self, amount, destination):
        """Governor payout through the 3-day timelock (clock restored afterwards)."""
        sender, t = self.vm.sender, self.t
        self.vm.sender = self.governor
        self.c.propose_claim_treasury(amount, destination)
        self.advance(TIMELOCK)
        self.c.execute_claim_treasury()
        self.t = t
        self.vm.warp(iso(t))
        self.vm.sender = sender

    def finalize(self, borrower):
        self.vm.sender = self.rando
        self.c.finalize_score(borrower)

    def record_loan(self, borrower, amount, lender=None, fee=FEE):
        who = lender if lender is not None else self.lender
        self.vm.sender = who
        self.vm.value = fee
        try:
            self.c.record_loan(borrower, amount, fee)
        finally:
            self.vm.value = 0
        self.deposited += fee
        self.loan_pairs.add((borrower, who))

    def close_loan(self, borrower, lender=None):
        self.vm.sender = lender if lender is not None else self.lender
        self.c.close_loan(borrower)

    def report(self, borrower, lender=None, url=LENDER_EVIDENCE):
        """Lender reports a default: only starts the dispute window."""
        who = lender if lender is not None else self.lender
        self.vm.sender = who
        self.c.report_default(borrower, url)
        self.loan_pairs.add((borrower, who))

    def finalize_default(self, borrower, lender=None):
        self.vm.sender = self.rando
        self.c.finalize_default(borrower, lender if lender is not None else self.lender)

    def default(self, borrower, lender=None):
        """Full default path: report, let the evaluator's window lapse, settle."""
        self.report(borrower, lender)
        self.advance(DEFAULT_WINDOW)
        self.finalize_default(borrower, lender)

    def withdraw(self, who):
        self.vm.sender = who
        return self.c.withdraw()

    # -- invariants ----------------------------------------------------
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
        """active_exposure == unlent capacity of live records + open-loan exposure."""
        for e in evaluators:
            caps = 0
            for b in borrowers:
                try:
                    sc = self.c.get_score(b)
                except Exception:
                    continue
                if sc["evaluator"] == e.as_hex:
                    caps += sc["exposure"]
            loans = sum(
                self.c.get_loan(b, l)["amount"]
                for (b, l) in self.loan_pairs
                if self.c.get_loan(b, l)["evaluator"] == e.as_hex
            )
            assert self.c.get_loan_exposure(e) == loans
            assert self.c.get_exposure(e) == caps + loans


@pytest.fixture
def env(request, direct_vm, direct_deploy, monkeypatch):
    marker = request.node.get_closest_marker("min_stake")  # dust tests deploy a small minimum
    c = direct_deploy(CONTRACT, marker.args[0] if marker else MIN_STAKE, BOND)
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
    e.lender2 = Address(b"\x12" * 20)
    e.rando = Address(b"\x22" * 20)
    e.many = [Address(bytes([0x40 + i]) * 20) for i in range(8)]
    direct_vm.sender = e.governor
    e.timelocked_lender(e.lender)
    e.timelocked_lender(e.lender2)
    for host in ("data.example.com", "counter.example.com", "evidence.example.com"):
        c.whitelist_domain(host)
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


def _lend(env, evaluator, borrower, score, amount, lender=None, fee=FEE):
    """Score a borrower, finalize it and record one loan against it."""
    _finalize(env, evaluator, borrower, score)
    env.record_loan(borrower, amount, lender, fee)


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

BAD_URLS = [
    "http://127.0.0.1/x", "http://localhost/x", "http://sub.localhost/x",
    "http://169.254.169.254/latest/meta-data", "http://10.0.0.1/x",
    "http://192.168.1.1/x", "http://172.16.0.1/x", "http://0.0.0.0/x",
    "http://[::1]/x", "http://[::ffff:127.0.0.1]/x", "http://2130706433/x",
    "http://0x7f000001/x", "http://user:pw@example.com/x",
    "ftp://example.com/x", "file:///etc/passwd", "gopher://example.com",
    "HTTP://example.com/x", "javascript:alert(1)", "http:///x", "",
    "http://example.com/a b", "http://service.internal/x",
]


@pytest.mark.parametrize("url", BAD_URLS)
def test_ssrf_rejected(env, alice, bob, url):
    env.stake(alice, MIN_STAKE)
    env.mock_score(500)
    env.vm.sender = alice
    with env.vm.expect_revert("Unsafe data_url"):
        env.c.request_score_update(bob, url)


@pytest.mark.parametrize("url", [u for u in BAD_URLS if u])
def test_ssrf_rejected_for_counter_url(env, alice, bob, carol, url):
    env.stake(alice, MIN_STAKE)
    env.request(alice, bob, 500)
    with env.vm.expect_revert("Unsafe counter_url"):
        env.challenge(carol, bob, counter_url=url)
    env.vm.value = 0
    assert env.c.get_score(bob)["state"] == "PENDING"


def test_ssrf_allows_public(env, alice, bob):
    env.stake(alice, MIN_STAKE)
    env.mock_score(500)
    env.vm.sender = env.governor
    env.c.whitelist_domain("93.184.216.34")
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
    assert s["tier"] == 2 and s["score"] == 700 and s["capacity_bps"] == 8000
    assert s["state"] == "PENDING" and s["challenger"] == ""
    assert s["timestamp"] == int(T0.timestamp())
    assert s["exposure"] == 160 * GEN and s["loaned"] == 0

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

    env.request(alice, env.many[0], 100)
    with env.vm.expect_revert("own score"):
        env.challenge(alice, env.many[0])
    with env.vm.expect_revert("exactly the challenge bond"):
        env.challenge(carol, env.many[0], value=BOND - 1)
    env.vm.value = 0
    env.assert_zero_wei([alice])


# ======================================================================
# dispute: overturned / upheld (median + widened threshold)
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

    env.resolve(bob, 600)  # tier 3 -> tier 1: gap 2 >= OVERTURN_TIER_GAP
    s = env.c.get_score(bob)
    assert s["state"] == "FINAL" and s["tier"] == 1 and s["score"] == 500

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


@pytest.mark.parametrize("new_score", [1000, 900, 850, 849, 700])
def test_dispute_upheld(env, alice, bob, carol, new_score):
    stake = 200 * GEN
    env.stake(alice, stake)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)
    env.resolve(bob, new_score)  # re-run in tier 3 or tier 2: gap <= 1, upheld

    s = env.c.get_score(bob)
    assert s["state"] == "FINAL" and s["tier"] == 3 and s["score"] == 850
    assert env.c.get_stake(alice) == stake
    assert env.c.get_treasury() == BOND
    assert env.c.get_claimable(carol) == 0
    env.assert_zero_wei([alice])


def test_overturn_boundary(env, alice, bob, carol, dave):
    env.stake(alice, 300 * GEN)
    env.request(alice, bob, 600)                     # tier 1 (capacity 150)
    env.request(alice, dave, 600)
    env.challenge(carol, bob)
    env.challenge(carol, dave)
    env.resolve(bob, 700)                            # tier 2: gap 1 -> upheld
    assert env.c.get_score(bob)["tier"] == 1
    env.resolve(dave, 850)                           # tier 3: gap 2 -> overturned
    assert env.c.get_score(dave)["tier"] == 3
    assert env.c.get_claimable(carol) == 2 * BOND
    env.assert_zero_wei([alice])


def test_boundary_noise_never_slashes(env, alice, bob, carol):
    """A re-run that lands one tier away (499 vs 600) is boundary noise: upheld."""
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 600)
    env.challenge(carol, bob)
    env.resolve(bob, 499)
    s = env.c.get_score(bob)
    assert s["tier"] == 1 and s["score"] == 500 and s["state"] == "FINAL"
    assert env.c.get_stake(alice) == 200 * GEN
    assert env.c.get_claimable(carol) == 0
    env.assert_zero_wei([alice])


def test_dispute_single_tier_disagreement_never_overturns(env, alice, bob, carol):
    """The worst honest disagreement (one tier) stays below OVERTURN_TIER_GAP."""
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 500)
    env.challenge(carol, bob)
    env.resolve(bob, 300 + 100)                   # re-run one tier lower: still upheld
    assert env.c.get_score(bob)["score"] == 500
    assert env.c.get_claimable(carol) == 0
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
# counter-evidence  [#4]
# ======================================================================

def _web_by_url(env, mapping):
    env.vm.clear_mocks()
    for pattern, (status, body) in mapping.items():
        env.vm.mock_web(pattern, {"status": status, "body": body})


def test_counter_url_is_stored(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 900)
    env.challenge(carol, bob, counter_url=COUNTER)
    assert env.c.get_score(bob)["counter_url"] == COUNTER


def test_dispute_weighs_both_sources_and_overturns(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 900)
    env.challenge(carol, bob, counter_url=COUNTER)

    _web_by_url(env, {
        r"data\.example\.com": (200, "evaluator-says-rich"),
        r"counter\.example\.com": (200, "challenger-shows-fraud"),
    })
    # Only a prompt carrying BOTH sources reaches the low-score answer.
    env.vm.mock_llm(r"(?s).*Source A.*evaluator-says-rich.*Source B.*challenger-shows-fraud.*", llm_json(100, "fraud"))
    env.vm.mock_llm(r".*Solvency Score.*", llm_json(900, "looks fine"))
    env.vm.sender = env.rando
    env.c.resolve_dispute(bob)

    s = env.c.get_score(bob)
    assert s["tier"] == 0 and s["reasoning"] == "fraud"
    assert env.c.get_claimable(carol) == 2 * BOND
    env.assert_zero_wei([alice])


def test_dispute_without_counter_url_uses_only_source_a(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)  # no counter-evidence
    _web_by_url(env, {r".*": (200, "evaluator-says-rich")})
    env.vm.mock_llm(r"(?s).*Source B \(challenger's counter-evidence\):.*", llm_json(0, "should not see source B"))
    env.vm.mock_llm(r".*Solvency Score.*", llm_json(880, "fine"))
    env.vm.sender = env.rando
    env.c.resolve_dispute(bob)
    assert env.c.get_score(bob)["tier"] == 3        # upheld
    assert env.c.get_claimable(carol) == 0


def test_unreachable_counter_url_does_not_slash_evaluator(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 900)
    env.challenge(carol, bob, counter_url=COUNTER)
    _web_by_url(env, {
        r"data\.example\.com": (200, "evaluator-says-rich"),
        r"counter\.example\.com": (404, "gone"),
    })
    env.vm.mock_llm(r".*Solvency Score.*", llm_json(880, "fine"))
    env.vm.sender = env.rando
    env.c.resolve_dispute(bob)                      # resolves normally on Source A alone
    assert env.c.get_score(bob)["tier"] == 3
    assert env.c.get_stake(alice) == 200 * GEN
    assert env.c.get_treasury() == BOND             # failed challenge forfeits the bond
    env.assert_zero_wei([alice])


# ======================================================================
# evidence unavailable: 404/410 slash, transient errors revert  [#3, trap]
# ======================================================================

@pytest.mark.parametrize("status,body", [(404, "nope"), (410, "gone"), (200, "")])
def test_missing_evidence_slashes_evaluator(env, alice, bob, carol, status, body):
    stake = 200 * GEN
    env.stake(alice, stake)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)

    env.mock_web_status(status, body)
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


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 403, 401])
def test_transient_http_error_reverts_without_slashing(env, alice, bob, carol, status):
    stake = 200 * GEN
    env.stake(alice, stake)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)
    env.mock_web_status(status)
    env.vm.sender = env.rando
    with env.vm.expect_revert("TRANSIENT"):
        env.c.resolve_dispute(bob)
    s = env.c.get_score(bob)
    assert s["state"] == "DISPUTED" and s["tier"] == 3        # untouched
    assert env.c.get_stake(alice) == stake
    assert env.c.get_accounting()["total_escrow"] == BOND     # bond still safely escrowed
    assert env.c.get_claimable(carol) == 0
    env.assert_zero_wei([alice])

    env.resolve(bob, 880)                                     # challenger retries later: resolves
    assert env.c.get_score(bob)["state"] == "FINAL"
    env.assert_zero_wei([alice])


def test_unreachable_network_is_transient_not_slashing(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)
    env.vm.clear_mocks()  # nothing mocked -> the fetch itself raises
    env.vm.sender = env.rando
    with env.vm.expect_revert("TRANSIENT"):
        env.c.resolve_dispute(bob)
    assert env.c.get_stake(alice) == 200 * GEN
    assert env.c.get_score(bob)["state"] == "DISPUTED"


def test_persistent_transient_failure_counts_after_timeout(env, alice, bob, carol):
    """An evaluator cannot dodge a dispute by serving 503 forever."""
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)
    env.mock_web_status(503)
    env.vm.sender = env.rando
    env.warp(timedelta(days=7) - EPS)
    with env.vm.expect_revert("TRANSIENT"):
        env.c.resolve_dispute(bob)
    env.warp(timedelta(days=7))
    env.c.resolve_dispute(bob)
    s = env.c.get_score(bob)
    assert s["score"] == 0 and s["state"] == "FINAL"
    assert env.c.get_claimable(carol) == 2 * BOND
    env.assert_zero_wei([alice])


def test_missing_evidence_with_unbonded_stake_still_slashes(env, alice, bob, carol):
    env.stake(alice, MIN_STAKE)
    env.request(alice, bob, 400)                 # tier 0: no capacity
    env.vm.sender = alice
    env.c.initiate_unstake(MIN_STAKE)            # everything is in the unbonding queue
    env.challenge(carol, bob)
    env.mock_web_status(404)
    env.vm.sender = env.rando
    env.c.resolve_dispute(bob)
    assert env.c.get_stake(alice) == 0           # slash 50 + reward 5 come out of unbonding
    assert env.c.get_unbonding(alice)["amount"] == MIN_STAKE - MIN_STAKE // 2 - BOND
    env.assert_zero_wei([alice])


# ======================================================================
# validator consensus on leader errors  [#2]
# ======================================================================

def _captured_validator(env, alice, bob):
    """Run a successful analysis so a validator closure is captured."""
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 500)


def _validate_leader_error(env, message, validator_sees):
    """validator_sees: HTTP status int, 'ok' (200 + good LLM) or 'network' (fetch raises)."""
    if validator_sees == "ok":
        env.mock_score(500)
    elif validator_sees == "network":
        env.vm.clear_mocks()
    else:
        env.mock_web_status(validator_sees)
    return env.vm.run_validator(leader_error=Exception(message))


@pytest.mark.parametrize("seen,agrees", [
    (404, True), (410, True), ("ok", False), (503, False), ("network", False),
])
def test_validator_agrees_on_data_unavailable_only_if_it_sees_it_too(env, alice, bob, seen, agrees):
    _captured_validator(env, alice, bob)
    assert _validate_leader_error(env, DATA_ERR, seen) is agrees


@pytest.mark.parametrize("seen,agrees", [
    (503, True), (429, True), (500, True), ("network", True), (404, False), ("ok", False),
])
def test_validator_agrees_on_transient_error_only_if_it_sees_it_too(env, alice, bob, seen, agrees):
    _captured_validator(env, alice, bob)
    assert _validate_leader_error(env, TRANSIENT_ERR, seen) is agrees


@pytest.mark.parametrize("message", ["[LLM_ERROR] Missing 'score'", "[EXPECTED] whatever", "boom", ""])
@pytest.mark.parametrize("seen", [404, 503, "ok"])
def test_validator_rejects_unclassified_leader_errors(env, alice, bob, message, seen):
    _captured_validator(env, alice, bob)
    assert _validate_leader_error(env, message, seen) is False


def test_validator_requires_exact_tier(env, alice, bob):
    _captured_validator(env, alice, bob)
    env.mock_score(600)                                                              # tier 1
    assert env.vm.run_validator(leader_result={"tier": 1, "reasoning": "r"}) is True
    assert env.vm.run_validator(leader_result={"tier": 2, "reasoning": "r"}) is False
    assert env.vm.run_validator(leader_result={"tier": 0, "reasoning": "r"}) is False
    env.mock_web_status(404)
    assert env.vm.run_validator(leader_result={"tier": 1, "reasoning": "r"}) is False  # validator errors


# ======================================================================
# lender whitelist & loan registry  [#1, #7, #8]
# ======================================================================

def test_whitelist_is_governor_only(env, carol):
    env.vm.sender = carol
    with env.vm.expect_revert("Only governor"):
        env.c.propose_whitelist_lender(carol)
    with env.vm.expect_revert("Only governor"):
        env.c.execute_whitelist_lender(carol)
    with env.vm.expect_revert("Only governor"):
        env.c.remove_lender(env.lender)
    assert not env.c.is_lender(carol)
    assert env.c.is_lender(env.lender)


def test_governor_cannot_whitelist_itself(env):
    env.vm.sender = env.governor
    with env.vm.expect_revert("Invalid lender"):
        env.c.propose_whitelist_lender(env.governor)
    assert not env.c.is_lender(env.governor)


def test_governor_cannot_record_loan_or_default_without_being_a_lender(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    env.vm.sender = env.governor
    with env.vm.expect_revert("Only whitelisted lender"):
        env.c.record_loan(bob, 10 * GEN, FEE)
    with env.vm.expect_revert("No open loan"):
        env.c.report_default(bob, LENDER_EVIDENCE)
    assert env.c.get_stake(alice) == 200 * GEN
    assert env.transfers == []


def test_record_loan_requires_whitelisted_lender(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    for attacker in (env.rando, carol, bob, alice):
        env.vm.sender = attacker
        with env.vm.expect_revert("Only whitelisted lender"):
            env.c.record_loan(bob, 1, FEE)
    env.vm.sender = env.governor
    env.c.remove_lender(env.lender)
    with env.vm.expect_revert("Only whitelisted lender"):
        env.record_loan(bob, 1)


def test_record_loan_rejections(env, alice, bob):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 800)
    with env.vm.expect_revert("exceeds borrow power"):   # PENDING: no power yet
        env.record_loan(bob, 1)
    env.advance(timedelta(hours=25))
    env.finalize(bob)
    with env.vm.expect_revert("Amount must be positive"):
        env.record_loan(bob, 0)
    with env.vm.expect_revert("exceeds borrow power"):
        env.record_loan(bob, 160 * GEN + 1)
    with env.vm.expect_revert("No score for borrower"):
        env.record_loan(alice, 1)
    env.timelocked_lender(alice)
    env.timelocked_lender(bob)
    with env.vm.expect_revert("cannot be evaluator or borrower"):
        env.record_loan(bob, 1, lender=alice)
    with env.vm.expect_revert("cannot be evaluator or borrower"):
        env.record_loan(bob, 1, lender=bob)
    env.assert_zero_wei([alice])


def test_record_loan_pins_capacity(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)                       # capacity 160
    assert env.c.get_max_borrow_power(bob) == 160 * GEN
    env.record_loan(bob, 100 * GEN)
    s = env.c.get_score(bob)
    assert s["exposure"] == 60 * GEN and s["loaned"] == 100 * GEN
    assert env.c.get_exposure(alice) == 160 * GEN         # total commitment unchanged
    assert env.c.get_loan_exposure(alice) == 100 * GEN
    assert env.c.get_max_borrow_power(bob) == 60 * GEN
    loan = env.c.get_loan(bob, env.lender)
    assert loan["amount"] == 100 * GEN and loan["evaluator"] == alice.as_hex
    env.record_loan(bob, 60 * GEN, lender=env.lender2)
    assert env.c.get_max_borrow_power(bob) == 0
    with env.vm.expect_revert("exceeds borrow power"):
        env.record_loan(bob, 1)
    env.assert_exposure_invariant([alice], [bob])
    env.assert_zero_wei([alice])


def test_same_lender_topup_accumulates(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    env.record_loan(bob, 40 * GEN)
    env.record_loan(bob, 30 * GEN)
    assert env.c.get_loan(bob, env.lender)["amount"] == 70 * GEN
    env.assert_exposure_invariant([alice], [bob])


def test_default_settles_only_callers_loan(env, alice, bob):
    """Single-lender wipeout fix: other lenders' loans stay intact and collectable."""
    stake = 200 * GEN
    env.stake(alice, stake)
    _finalize(env, alice, bob, 800)                       # capacity 160
    env.record_loan(bob, 100 * GEN)                       # lender 1
    env.record_loan(bob, 60 * GEN, lender=env.lender2)    # lender 2

    env.default(bob)                                      # lender 1 defaults first
    assert env.transfers == [(env.lender, 100 * GEN)]
    assert env.c.get_stake(alice) == stake - 100 * GEN
    assert env.c.get_loan(bob, env.lender)["amount"] == 0
    assert env.c.get_loan(bob, env.lender2)["amount"] == 60 * GEN        # untouched
    assert env.c.get_loan_exposure(alice) == 60 * GEN
    assert env.c.get_exposure(alice) == 60 * GEN                          # 100 released
    assert env.c.get_score(bob)["score"] == 0                             # no new borrowing
    assert env.c.get_max_borrow_power(bob) == 0
    env.assert_exposure_invariant([alice], [bob])
    env.assert_zero_wei([alice])

    env.default(bob, lender=env.lender2)                  # lender 2 still collects in full
    assert env.transfers[-1] == (env.lender2, 60 * GEN)
    assert env.c.get_stake(alice) == stake - 160 * GEN
    assert env.c.get_exposure(alice) == 0
    env.assert_exposure_invariant([alice], [bob])
    env.assert_zero_wei([alice])


def test_default_requires_an_open_loan(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    _lend(env, alice, bob, 800, 50 * GEN)
    for attacker in (env.rando, carol, bob, alice, env.governor, env.lender2):   # lender2 has no loan
        env.vm.sender = attacker
        with env.vm.expect_revert("No open loan"):
            env.c.report_default(bob, LENDER_EVIDENCE)
    with env.vm.expect_revert("No open loan"):
        env.c.report_default(carol, LENDER_EVIDENCE)      # borrower with no score/loans at all
    assert env.c.get_stake(alice) == 200 * GEN
    assert env.transfers == []
    env.assert_zero_wei([alice])


def test_cannot_default_twice(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _lend(env, alice, bob, 800, 50 * GEN)
    env.default(bob)
    with env.vm.expect_revert("No open loan"):
        env.default(bob)
    assert env.transfers == [(env.lender, 50 * GEN)]
    env.assert_zero_wei([alice])


def test_default_cannot_exceed_recorded_amount(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _lend(env, alice, bob, 800, 25 * GEN)
    env.default(bob)
    assert env.transfers == [(env.lender, 25 * GEN)]      # exactly the recorded loan
    assert env.c.get_stake(alice) == 175 * GEN


def test_closed_loan_cannot_default(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _lend(env, alice, bob, 800, 100 * GEN)
    env.vm.sender = env.rando
    with env.vm.expect_revert("No open loan"):
        env.c.close_loan(bob)                             # only the lender can close its own
    env.close_loan(bob)
    assert env.c.get_loan_exposure(alice) == 0
    assert env.c.get_exposure(alice) == 60 * GEN          # capacity left, loan released
    with env.vm.expect_revert("No open loan"):
        env.default(bob)
    env.assert_exposure_invariant([alice], [bob])
    env.assert_zero_wei([alice])


def test_loan_on_other_evaluator_requires_closing_first(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.stake(bob, 200 * GEN)
    _lend(env, alice, carol, 800, 160 * GEN)              # capacity fully lent -> power 0
    assert env.c.get_max_borrow_power(carol) == 0
    env.request(bob, carol, 700)                          # any evaluator may replace [#5]
    env.advance(timedelta(hours=25))
    env.finalize(carol)
    with env.vm.expect_revert("Close the existing loan first"):
        env.record_loan(carol, 10 * GEN)
    assert env.c.get_loan(carol, env.lender)["evaluator"] == alice.as_hex
    env.record_loan(carol, 10 * GEN, lender=env.lender2)  # a different lender is fine
    assert env.c.get_loan(carol, env.lender2)["evaluator"] == bob.as_hex
    env.assert_exposure_invariant([alice, bob], [carol])


# -- loans outlive the score  [#8] ---------------------------------------

def test_loan_protection_outlives_score_expiry(env, alice, bob):
    stake = 200 * GEN
    env.stake(alice, stake)
    _lend(env, alice, bob, 800, 100 * GEN)                # capacity 160 -> 60 + loan 100
    env.warp(timedelta(days=90))                          # score long expired
    assert env.c.get_max_borrow_power(bob) == 0
    env.vm.sender = env.rando
    env.c.release_expired(bob)                            # frees unlent capacity only
    assert env.c.get_score(bob)["exposure"] == 0
    assert env.c.get_exposure(alice) == 100 * GEN         # the loan is still pinned
    env.vm.sender = alice
    with env.vm.expect_revert("below exposure"):
        env.c.initiate_unstake(101 * GEN)                 # cannot walk away from the loan
    env.default(bob)                                      # day 90: still collectable
    assert env.transfers == [(env.lender, 100 * GEN)]
    assert env.c.get_stake(alice) == stake - 100 * GEN
    env.assert_exposure_invariant([alice], [bob])
    env.assert_zero_wei([alice])


def test_loan_protection_ends_after_loan_ttl(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _lend(env, alice, bob, 800, 100 * GEN)
    created = env.c.get_loan(bob, env.lender)["created"]
    env.warp(timedelta(hours=25) + LOAN_TTL)              # exactly created + TTL: still protected
    assert env.t.timestamp() == created + int(LOAN_TTL.total_seconds())
    env.vm.sender = env.rando
    with env.vm.expect_revert("Loan has not expired"):
        env.c.expire_loan(bob, env.lender)
    env.warp(timedelta(hours=25) + LOAN_TTL + EPS)
    env.vm.sender = env.lender
    with env.vm.expect_revert("Loan protection expired"):
        env.c.report_default(bob, LENDER_EVIDENCE)
    env.vm.sender = env.rando
    env.c.expire_loan(bob, env.lender)                    # anyone frees the stuck stake
    assert env.c.get_loan_exposure(alice) == 0
    with env.vm.expect_revert("No open loan"):
        env.c.expire_loan(bob, env.lender)
    env.assert_exposure_invariant([alice], [bob])
    env.assert_zero_wei([alice])


def test_loan_on_expired_score_is_rejected(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    env.warp(TTL + EPS)
    with env.vm.expect_revert("exceeds borrow power"):
        env.record_loan(bob, 1)


# -- under-collateralization after a dispute slash  [#6] ------------------

def test_payouts_capped_first_come_first_served_after_dispute_slash(env, alice, bob, dave, carol):
    env.stake(alice, MIN_STAKE)                           # 100
    env.request(alice, bob, 500)                          # capacity 50
    env.advance(timedelta(hours=25))
    env.finalize(bob)
    env.record_loan(bob, 30 * GEN)                        # lender 1
    env.record_loan(bob, 20 * GEN, lender=env.lender2)    # lender 2
    env.request(alice, dave, 100)                         # capacity 10: committed 60 <= 100
    env.challenge(carol, dave)
    env.resolve(dave, 700)                                # overturned: slash 50 + reward 5
    assert env.c.get_stake(alice) == 45 * GEN             # < exposure (loans 50 + dave's 10)
    assert env.c.get_exposure(alice) > env.c.get_stake(alice)

    env.default(bob)                                      # first: paid in full
    assert env.transfers[-1] == (env.lender, 30 * GEN)
    assert env.c.get_stake(alice) == 15 * GEN
    env.default(bob, lender=env.lender2)                  # second: only what is left
    assert env.transfers[-1] == (env.lender2, 15 * GEN)
    assert env.c.get_stake(alice) == 0
    assert env.c.get_loan_exposure(alice) == 0            # both fully released regardless
    env.assert_exposure_invariant([alice], [bob, dave])
    env.assert_zero_wei([alice])


def test_default_can_take_the_entire_stake_and_evaluator_can_restake(env, alice, bob):
    env.stake(alice, MIN_STAKE)
    _lend(env, alice, bob, 1000, 100 * GEN)
    env.default(bob)                                      # takes the whole 100
    assert env.c.get_stake(alice) == 0
    env.stake(alice, MIN_STAKE)
    env.request(alice, env.many[0], 1000)
    assert env.c.get_exposure(alice) == 100 * GEN
    env.assert_zero_wei([alice])


# -- treasury claim route  [#8] -----------------------------------------

def _fund_treasury(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)
    env.resolve(bob, 880)                                 # upheld -> bond to treasury
    assert env.c.get_treasury() == BOND


def test_claim_treasury(env, alice, bob, carol):
    _fund_treasury(env, alice, bob, carol)
    env.claim_treasury(3 * GEN, env.many[0])
    assert env.c.get_treasury() == 2 * GEN
    assert env.transfers == [(env.many[0], 3 * GEN)]
    env.assert_zero_wei([alice])
    env.claim_treasury(2 * GEN, env.many[0])
    assert env.c.get_treasury() == 0
    env.assert_zero_wei([alice])


def test_claim_treasury_rejections(env, alice, bob, carol):
    _fund_treasury(env, alice, bob, carol)
    from genlayer import Address

    for attacker in (env.rando, carol, env.lender):
        env.vm.sender = attacker
        with env.vm.expect_revert("Only governor"):
            env.c.propose_claim_treasury(1, attacker)
    env.vm.sender = env.governor
    with env.vm.expect_revert("Invalid amount"):
        env.c.propose_claim_treasury(0, env.many[0])
    with env.vm.expect_revert("Invalid amount"):
        env.c.propose_claim_treasury(BOND + 1, env.many[0])
    with env.vm.expect_revert("Invalid destination"):
        env.c.propose_claim_treasury(1, env.governor)             # no self-payout
    with env.vm.expect_revert("Invalid destination"):
        env.c.propose_claim_treasury(1, Address(b"\x00" * 20))
    assert env.c.get_treasury() == BOND
    assert env.transfers == []
    env.assert_zero_wei([alice])


def test_claim_treasury_cannot_touch_stake_or_default_proceeds(env, alice, bob, carol):
    _fund_treasury(env, alice, bob, carol)
    env.advance(timedelta(hours=25))
    env.vm.sender = env.governor
    with env.vm.expect_revert("Invalid amount"):
        env.c.propose_claim_treasury(BOND + 1, env.many[0])              # stake is not claimable
    assert env.c.get_stake(alice) == 200 * GEN


# ======================================================================
# replacement rules  [#5 and front-running]
# ======================================================================

def test_score_with_zero_power_is_replaceable_by_any_evaluator(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.stake(bob, 200 * GEN)
    _finalize(env, alice, carol, 0)                       # zero score: no borrow power
    assert env.c.get_max_borrow_power(carol) == 0
    env.request(bob, carol, 600)                          # immediately, within the 30 days
    s = env.c.get_score(carol)
    assert s["evaluator"] == bob.as_hex and s["tier"] == 1
    env.assert_exposure_invariant([alice, bob], [carol])
    env.assert_zero_wei([alice, bob])


def test_fully_lent_score_is_replaceable_but_loans_stay_pinned(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.stake(bob, 200 * GEN)
    _lend(env, alice, carol, 800, 160 * GEN)              # whole capacity lent out
    env.request(bob, carol, 100)                          # replaced right away
    assert env.c.get_score(carol)["evaluator"] == bob.as_hex
    assert env.c.get_loan_exposure(alice) == 160 * GEN    # alice's liability did not vanish
    env.default(carol)
    assert env.transfers == [(env.lender, 160 * GEN)]
    assert env.c.get_stake(alice) == 40 * GEN
    env.assert_zero_wei([alice, bob])


def test_active_score_with_capacity_cannot_be_lowered_before_expiry(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    for new_score in (0, 100, 799, 800):          # lower or equal: never allowed
        env.mock_score(new_score)
        env.vm.sender = alice
        with env.vm.expect_revert("cannot be replaced before expiry"):
            env.c.request_score_update(bob, URL)
    s = env.c.get_score(bob)
    assert s["tier"] == 2 and s["exposure"] == 160 * GEN
    env.assert_zero_wei([alice])


def test_score_replaceable_exactly_after_expiry(env, alice, bob):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 800)
    env.advance(timedelta(hours=25))
    env.finalize(bob)
    env.warp(TTL)
    env.mock_score(0)
    env.vm.sender = alice
    with env.vm.expect_revert("cannot be replaced before expiry"):
        env.c.request_score_update(bob, URL)
    env.warp(TTL + EPS)
    env.c.request_score_update(bob, URL)
    assert env.c.get_score(bob)["score"] == 0
    assert env.c.get_exposure(alice) == 0


def test_other_evaluator_cannot_overwrite_active_score(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.stake(bob, 200 * GEN)
    _finalize(env, alice, carol, 800)
    env.mock_score(100)
    env.vm.sender = bob
    with env.vm.expect_revert("belongs to another evaluator"):
        env.c.request_score_update(carol, URL)
    s = env.c.get_score(carol)
    assert s["evaluator"] == alice.as_hex and s["tier"] == 2
    assert env.c.get_exposure(alice) == 160 * GEN
    assert env.c.get_exposure(bob) == 0
    env.assert_zero_wei([alice, bob])


def test_cannot_overwrite_pending_or_disputed(env, alice, bob, carol, dave):
    env.stake(alice, 200 * GEN)
    env.stake(bob, 200 * GEN)
    env.request(alice, carol, 800)
    env.mock_score(100)
    for who in (bob, alice):
        env.vm.sender = who
        with env.vm.expect_revert("still in challenge process"):
            env.c.request_score_update(carol, URL)
    env.challenge(dave, carol)
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
    assert env.c.get_exposure(alice) == 0
    assert env.c.get_exposure(bob) == 100 * GEN
    env.assert_exposure_invariant([alice, bob], [carol])
    env.assert_zero_wei([alice, bob])


def test_replaced_after_expiry_frees_old_exposure(env, alice, bob):
    env.stake(alice, MIN_STAKE)
    _finalize(env, alice, bob, 1000)
    assert env.c.get_exposure(alice) == 100 * GEN
    env.warp(TTL + timedelta(hours=1))
    env.request(alice, bob, 600)
    assert env.c.get_exposure(alice) == 50 * GEN
    assert env.c.get_score(bob)["state"] == "PENDING"
    env.assert_exposure_invariant([alice], [bob])


# ======================================================================
# cumulative exposure
# ======================================================================

def test_exposure_cannot_exceed_stake(env, alice):
    b = env.many
    env.stake(alice, MIN_STAKE)
    env.request(alice, b[0], 600)
    assert env.c.get_exposure(alice) == 50 * GEN
    env.mock_score(700)                                   # tier 2 would commit 80 more
    env.vm.sender = alice
    with env.vm.expect_revert("Exposure exceeds stake"):
        env.c.request_score_update(b[1], URL)
    assert env.c.get_exposure(alice) == 50 * GEN
    env.request(alice, b[1], 500)
    assert env.c.get_exposure(alice) == 100 * GEN
    env.mock_score(500)
    env.vm.sender = alice
    with env.vm.expect_revert("Exposure exceeds stake"):
        env.c.request_score_update(b[2], URL)
    env.assert_exposure_invariant([alice], b)
    env.assert_zero_wei([alice])


def test_five_borrowers_cannot_leverage_100_gen_into_500(env, alice):
    b = env.many
    env.stake(alice, MIN_STAKE)
    ok = 0
    for borrower in b[:5]:
        env.mock_score(500)
        env.vm.sender = alice
        try:
            env.c.request_score_update(borrower, URL)
            ok += 1
        except Exception as ex:
            assert "Exposure exceeds stake" in str(ex)
    assert ok == 2
    assert env.c.get_exposure(alice) <= 100 * GEN
    env.assert_exposure_invariant([alice], b)


def test_exposure_counts_open_loans(env, alice):
    b = env.many
    env.stake(alice, MIN_STAKE)
    _lend(env, alice, b[0], 1000, 100 * GEN)              # whole stake lent
    env.request(alice, b[1], 0)                           # zero scores add nothing
    env.mock_score(500)
    env.vm.sender = alice
    with env.vm.expect_revert("Exposure exceeds stake"):
        env.c.request_score_update(b[2], URL)
    env.close_loan(b[0])                                  # loan repaid: capacity for others
    env.request(alice, b[2], 500)
    assert env.c.get_exposure(alice) == 50 * GEN
    env.assert_exposure_invariant([alice], b)


def test_exposure_is_per_evaluator(env, alice, bob):
    b = env.many
    env.stake(alice, MIN_STAKE)
    env.stake(bob, MIN_STAKE)
    env.request(alice, b[0], 1000)
    env.request(bob, b[1], 1000)
    assert env.c.get_exposure(alice) == 100 * GEN
    assert env.c.get_exposure(bob) == 100 * GEN
    env.assert_exposure_invariant([alice, bob], b)


def test_expired_exposure_can_be_released_and_reused(env, alice):
    b = env.many
    env.stake(alice, MIN_STAKE)
    _finalize(env, alice, b[0], 1000)
    env.mock_score(500)
    env.vm.sender = alice
    with env.vm.expect_revert("Exposure exceeds stake"):
        env.c.request_score_update(b[1], URL)
    env.vm.sender = env.rando
    with env.vm.expect_revert("has not expired"):
        env.c.release_expired(b[0])
    env.warp(TTL)
    with env.vm.expect_revert("has not expired"):
        env.c.release_expired(b[0])
    env.warp(TTL + timedelta(hours=1))
    env.c.release_expired(b[0])
    assert env.c.get_exposure(alice) == 0
    env.c.release_expired(b[0])
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
    env.request(alice, bob, 400)
    env.challenge(carol, bob)
    env.resolve(bob, 900)
    s = env.c.get_score(bob)
    assert s["tier"] == 3
    assert s["exposure"] == 0                              # an overturn never grows capacity
    assert env.c.get_exposure(alice) == s["exposure"]
    env.assert_zero_wei([alice])


def test_default_frees_exposure_and_blocks_new_loans(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _lend(env, alice, bob, 500, 40 * GEN)                 # capacity 100: 60 + loan 40
    assert env.c.get_exposure(alice) == 100 * GEN
    env.default(bob)
    assert env.c.get_exposure(alice) == 0
    with env.vm.expect_revert("exceeds borrow power"):
        env.record_loan(bob, 1, lender=env.lender2)       # defaulted borrower: no more credit
    env.assert_exposure_invariant([alice], [bob])


# ======================================================================
# premature borrowing & expiration
# ======================================================================

def test_pending_score_has_zero_borrow_power(env, alice, bob):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 800)
    assert env.c.get_max_borrow_power(bob) == 0
    env.warp(timedelta(hours=24))
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
    env.request(alice, bob, 800)
    env.warp(timedelta(hours=25))
    env.finalize(bob)
    env.warp(TTL)
    assert env.c.get_max_borrow_power(bob) == 160 * GEN
    env.warp(TTL + EPS)
    assert env.c.get_max_borrow_power(bob) == 0


def test_borrow_power_math_and_dynamic_drop(env, alice, bob):
    assert env.c.get_max_borrow_power(bob) == 0
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 600)
    assert env.c.get_max_borrow_power(bob) == 100 * GEN
    env.vm.sender = alice
    env.c.initiate_unstake(100 * GEN)
    assert env.c.get_max_borrow_power(bob) == 50 * GEN


def test_topup_cannot_inflate_borrow_power(env, alice, bob):
    env.stake(alice, MIN_STAKE)
    _finalize(env, alice, bob, 500)
    env.stake(alice, 900 * GEN)
    assert env.c.get_max_borrow_power(bob) == 50 * GEN


def test_borrow_power_floor_division(env, alice, bob):
    env.stake(alice, MIN_STAKE + 1)
    _finalize(env, alice, bob, 800)
    assert (8000 * (MIN_STAKE + 1)) % 10000 != 0
    assert env.c.get_max_borrow_power(bob) == (8000 * (MIN_STAKE + 1)) // 10000


# ======================================================================
# front-running gap: exposure evasion via unstake
# ======================================================================

def test_cannot_unstake_below_exposure(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    env.vm.sender = alice
    with env.vm.expect_revert("below exposure"):
        env.c.initiate_unstake(40 * GEN + 1)
    with env.vm.expect_revert("below exposure"):
        env.c.initiate_unstake(200 * GEN)
    env.c.initiate_unstake(40 * GEN)
    with env.vm.expect_revert("below exposure"):
        env.c.initiate_unstake(1)
    env.assert_zero_wei([alice])


def test_unstake_guard_counts_pending_scores_and_loans(env, alice):
    b = env.many
    env.stake(alice, 400 * GEN)
    _lend(env, alice, b[0], 600, 100 * GEN)               # capacity 200: 100 + loan 100
    env.request(alice, b[1], 600)                         # PENDING 200: committed 400
    env.stake(alice, 100 * GEN)                           # top-up leaves 100 GEN of slack
    env.vm.sender = alice
    with env.vm.expect_revert("below exposure"):
        env.c.initiate_unstake(101 * GEN)                 # 500 - 101 = 399 < 400
    env.c.initiate_unstake(100 * GEN)
    env.assert_exposure_invariant([alice], b)
    env.assert_zero_wei([alice])


def test_unstake_unlocks_after_loan_closed(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _lend(env, alice, bob, 800, 100 * GEN)
    env.vm.sender = alice
    with env.vm.expect_revert("below exposure"):
        env.c.initiate_unstake(41 * GEN)
    env.close_loan(bob)                                   # exposure 60 left (capacity)
    env.vm.sender = alice
    env.c.initiate_unstake(140 * GEN)
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
    env.c.initiate_unstake(200 * GEN)
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
    _finalize(env, alice, bob, 800)
    assert env.c.get_max_borrow_power(bob) == 800 * 300 * GEN // 1000
    _unstake(env, alice, 60 * GEN)
    assert env.c.get_stake(alice) == 240 * GEN
    u = env.c.get_unbonding(alice)
    assert u["amount"] == 60 * GEN
    assert u["unlock_time"] == int((T0 + timedelta(hours=25) + WEEK).timestamp())
    assert env.c.get_claimable(alice) == 0
    assert env.c.get_max_borrow_power(bob) == 800 * 240 * GEN // 1000
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
    _finalize(env, alice, bob, 600)
    _unstake(env, alice, 51 * GEN)
    env.mock_score(500)
    env.vm.sender = alice
    with env.vm.expect_revert("not a staked evaluator"):
        env.c.request_score_update(carol, URL)
    assert env.c.get_score(bob)["tier"] == 1
    assert env.c.get_max_borrow_power(bob) == 5000 * 99 * GEN // 10000
    env.assert_zero_wei([alice])


def test_default_slash_eats_unbonding(env, alice, bob, dave, carol):
    """A dispute slash can leave active stake below exposure; the default then
    reaches into the unbonding queue."""
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 500)                          # capacity 100
    env.advance(timedelta(hours=25))
    env.finalize(bob)
    env.record_loan(bob, 100 * GEN)                       # pinned loan 100
    env.request(alice, dave, 100)                         # capacity 20 -> committed 120
    env.vm.sender = alice
    env.c.initiate_unstake(80 * GEN)                      # leaves 120 == exposure
    env.challenge(carol, dave)
    env.resolve(dave, 700)                                # overturned: 50 + 5 from active
    assert env.c.get_stake(alice) == 65 * GEN
    env.default(bob)                                      # 100: 65 active + 35 unbonding
    assert env.transfers == [(env.lender, 100 * GEN)]
    assert env.c.get_stake(alice) == 0
    assert env.c.get_unbonding(alice)["amount"] == 45 * GEN
    env.assert_zero_wei([alice])
    env.warp(timedelta(days=9))
    env.vm.sender = alice
    assert env.c.claim_unstaked() == 45 * GEN
    env.assert_zero_wei([alice])


def test_default_within_active_leaves_unbonding_intact(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _lend(env, alice, bob, 800, 30 * GEN)                 # exposure 160
    env.vm.sender = alice
    env.c.initiate_unstake(40 * GEN)
    env.default(bob)
    assert env.c.get_stake(alice) == 130 * GEN
    assert env.c.get_unbonding(alice)["amount"] == 40 * GEN
    env.assert_zero_wei([alice])


def test_dispute_slash_eats_unbonding(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 100)
    _unstake(env, alice, 180 * GEN)
    env.challenge(carol, bob)
    env.resolve(bob, 700)
    left = 180 * GEN - 30 * GEN - BOND
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
    _unstake(env, alice, 5 * GEN)
    env.challenge(carol, bob)
    assert env.c.get_disputed_count(alice) == 1
    with env.vm.expect_revert("disputed scores"):
        _unstake(env, alice, 1)
    env.warp(timedelta(days=30))
    with env.vm.expect_revert("disputed scores"):
        _claim_unstaked(env, alice)
    env.assert_zero_wei([alice])
    env.resolve(bob, 380)
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
# default dispute window  [#1]
# ======================================================================

def _mock_default_verdict(env, defaulted, status=200, body="repayment-records", lender_status=200,
                          lender_body="default-claim-records"):
    """`status`/`body` describe the evaluator's evidence, `lender_*` the lender's."""
    env.vm.clear_mocks()
    env.vm.mock_web(r".*default-claim\.json.*", {"status": lender_status, "body": lender_body})
    env.vm.mock_web(r".*repayment\.json.*", {"status": status, "body": body})
    env.vm.mock_llm(
        r".*impartial judge.*",
        json.dumps(json.dumps({"defaulted": defaulted, "reasoning": "judged"})),
    )


def _dispute_default(env, evaluator, borrower, lender=None, url=EVIDENCE):
    env.vm.sender = evaluator
    env.c.dispute_default(borrower, lender if lender is not None else env.lender, url)


def _pending_default(env, alice, bob, amount=100 * GEN):
    env.stake(alice, 200 * GEN)
    _lend(env, alice, bob, 800, amount)
    env.report(bob)


def test_report_default_only_opens_a_dispute_window(env, alice, bob):
    _pending_default(env, alice, bob)
    loan = env.c.get_loan(bob, env.lender)
    assert loan["status"] == "PENDING_DEFAULT" and loan["amount"] == 100 * GEN
    assert loan["default_start"] == int(env.t.timestamp())
    assert env.c.get_stake(alice) == 200 * GEN        # nothing slashed yet
    assert env.transfers == []                         # nothing paid yet
    assert env.c.get_loan_exposure(alice) == 100 * GEN # exposure stays locked
    env.vm.sender = alice
    with env.vm.expect_revert("below exposure"):
        env.c.initiate_unstake(150 * GEN)
    env.assert_exposure_invariant([alice], [bob])
    env.assert_zero_wei([alice])


def test_finalize_default_only_after_the_window(env, alice, bob):
    _pending_default(env, alice, bob)
    env.advance(DEFAULT_WINDOW - EPS)
    with env.vm.expect_revert("Dispute window still open"):
        env.finalize_default(bob)
    env.advance(EPS)                                   # exactly the window: now settleable
    env.finalize_default(bob)
    assert env.transfers == [(env.lender, 100 * GEN)]
    assert env.c.get_stake(alice) == 100 * GEN
    assert env.c.get_loan(bob, env.lender)["amount"] == 0
    assert env.c.get_score(bob)["score"] == 0
    env.assert_exposure_invariant([alice], [bob])
    env.assert_zero_wei([alice])
    with env.vm.expect_revert("No open loan"):
        env.finalize_default(bob)


def test_report_default_cannot_be_repeated_or_bypassed_while_pending(env, alice, bob):
    _pending_default(env, alice, bob)
    env.vm.sender = env.lender
    with env.vm.expect_revert("Default already reported"):
        env.c.report_default(bob, LENDER_EVIDENCE)
    with env.vm.expect_revert("Default already reported"):
        env.c.close_loan(bob)                          # cannot sidestep the dispute window
    with pytest.raises(Exception, match="Default already reported"):
        env.record_loan(bob, 1 * GEN)                  # nor top up
    env.vm.sender = alice
    with env.vm.expect_revert("Default already reported"):
        env.c.request_loan_closure(bob, env.lender)
    env.vm.sender = env.rando
    with env.vm.expect_revert("Default already reported"):
        env.c.expire_loan(bob, env.lender)
    env.assert_zero_wei([alice])


def test_finalize_default_requires_a_pending_default(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _lend(env, alice, bob, 800, 100 * GEN)             # OPEN, not reported
    env.vm.sender = env.rando
    with env.vm.expect_revert("No pending default"):
        env.c.finalize_default(bob, env.lender)
    with env.vm.expect_revert("No open loan"):
        env.c.finalize_default(bob, env.lender2)


def test_fake_loan_default_is_overturned_and_lender_removed(env, alice, bob):
    """A malicious whitelisted address records a fake loan and reports a default."""
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    attacker = env.many[7]
    env.timelocked_lender(attacker)
    env.record_loan(bob, 160 * GEN, lender=attacker)
    env.report(bob, lender=attacker)
    assert env.transfers == []

    _mock_default_verdict(env, defaulted=False)
    _dispute_default(env, alice, bob, lender=attacker)

    assert env.transfers == []                                   # evaluator is safe
    assert env.c.get_stake(alice) == 200 * GEN
    assert env.c.get_loan(bob, attacker)["amount"] == 0          # claim cancelled
    assert env.c.get_loan_exposure(alice) == 0
    assert not env.c.is_lender(attacker)                         # penalised
    assert env.c.get_claimable(alice) == FEE                     # the fee is not refunded
    env.vm.sender = attacker
    with pytest.raises(Exception, match="Only whitelisted lender"):
        env.c.record_loan(bob, 1, FEE)
    env.assert_exposure_invariant([alice], [bob])
    env.assert_zero_wei([alice])
    with env.vm.expect_revert("No open loan"):
        env.finalize_default(bob, attacker)


def test_confirmed_default_settles_immediately(env, alice, bob):
    _pending_default(env, alice, bob, amount=100 * GEN)
    _mock_default_verdict(env, defaulted=True)
    _dispute_default(env, alice, bob)
    assert env.transfers == [(env.lender, 100 * GEN)]            # no need to wait out the window
    assert env.c.get_stake(alice) == 100 * GEN
    assert env.c.is_lender(env.lender)                           # honest lender keeps its status
    env.assert_zero_wei([alice])


@pytest.mark.parametrize("verdict_raw", ["false", "False", " FALSE "])
def test_string_false_verdict_is_parsed(env, alice, bob, verdict_raw):
    _pending_default(env, alice, bob)
    env.vm.clear_mocks()
    env.vm.mock_web(r".*", {"status": 200, "body": "x"})
    env.vm.mock_llm(r".*impartial judge.*", json.dumps(json.dumps({"defaulted": verdict_raw})))
    _dispute_default(env, alice, bob)
    assert env.c.get_loan(bob, env.lender)["amount"] == 0 and env.transfers == []
    assert not env.c.is_lender(env.lender)


def test_unavailable_dispute_evidence_does_not_disprove_the_default(env, alice, bob):
    _pending_default(env, alice, bob)
    _mock_default_verdict(env, defaulted=False, status=404)      # evaluator's own evidence is gone
    _dispute_default(env, alice, bob)
    assert env.transfers == [(env.lender, 100 * GEN)]
    env.assert_zero_wei([alice])


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_dispute_error_reverts_and_can_be_retried(env, alice, bob, status):
    _pending_default(env, alice, bob)
    _mock_default_verdict(env, defaulted=False, status=status)
    env.vm.sender = alice
    with env.vm.expect_revert("TRANSIENT"):
        env.c.dispute_default(bob, env.lender, EVIDENCE)
    assert env.c.get_loan(bob, env.lender)["status"] == "PENDING_DEFAULT"
    assert env.c.get_stake(alice) == 200 * GEN
    _mock_default_verdict(env, defaulted=False)                  # service recovers: retry succeeds
    _dispute_default(env, alice, bob)
    assert env.c.get_loan(bob, env.lender)["amount"] == 0 and env.transfers == []


def test_malformed_llm_verdict_reverts_without_deciding(env, alice, bob):
    _pending_default(env, alice, bob)
    env.vm.clear_mocks()
    env.vm.mock_web(r".*", {"status": 200, "body": "x"})
    env.vm.mock_llm(r".*impartial judge.*", json.dumps(json.dumps({"nonsense": 1})))
    env.vm.sender = alice
    with env.vm.expect_revert("LLM_ERROR"):
        env.c.dispute_default(bob, env.lender, EVIDENCE)
    assert env.c.get_loan(bob, env.lender)["status"] == "PENDING_DEFAULT"
    assert env.c.is_lender(env.lender)


def test_dispute_default_access_and_window(env, alice, bob, carol):
    _pending_default(env, alice, bob)
    _mock_default_verdict(env, defaulted=False)
    for attacker in (env.rando, carol, bob, env.lender, env.governor):
        env.vm.sender = attacker
        with env.vm.expect_revert("Only the backing evaluator"):
            env.c.dispute_default(bob, env.lender, EVIDENCE)
    env.vm.sender = alice
    with env.vm.expect_revert("No open loan"):
        env.c.dispute_default(bob, env.lender2, EVIDENCE)     # lender2 never lent
    env.advance(DEFAULT_WINDOW)                                  # exactly at the deadline: closed
    with env.vm.expect_revert("Dispute window closed"):
        env.c.dispute_default(bob, env.lender, EVIDENCE)
    env.finalize_default(bob)
    assert env.transfers == [(env.lender, 100 * GEN)]


def test_dispute_last_second_is_accepted(env, alice, bob):
    _pending_default(env, alice, bob)
    _mock_default_verdict(env, defaulted=False)
    env.advance(DEFAULT_WINDOW - EPS)
    _dispute_default(env, alice, bob)
    assert env.c.get_loan(bob, env.lender)["amount"] == 0


def test_dispute_default_evidence_must_be_safe_and_whitelisted(env, alice, bob):
    _pending_default(env, alice, bob)
    _mock_default_verdict(env, defaulted=False)
    env.vm.sender = alice
    with env.vm.expect_revert("Unsafe evaluator_evidence_url"):
        env.c.dispute_default(bob, env.lender, "http://127.0.0.1/x")
    with env.vm.expect_revert("Domain not whitelisted"):
        env.c.dispute_default(bob, env.lender, "https://evil.example.org/x")
    assert env.c.get_loan(bob, env.lender)["status"] == "PENDING_DEFAULT"


def test_other_lenders_unaffected_by_a_cancelled_default(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    env.record_loan(bob, 100 * GEN)
    env.record_loan(bob, 60 * GEN, lender=env.lender2)
    env.report(bob)
    _mock_default_verdict(env, defaulted=False)
    _dispute_default(env, alice, bob)                             # lender 1's claim cancelled
    assert env.c.get_loan(bob, env.lender2)["amount"] == 60 * GEN
    assert env.c.get_loan_exposure(alice) == 60 * GEN
    assert env.c.is_lender(env.lender2)
    env.default(bob, lender=env.lender2)                          # lender 2 still collects
    assert env.transfers == [(env.lender2, 60 * GEN)]
    env.assert_exposure_invariant([alice], [bob])
    env.assert_zero_wei([alice])


# -- validator behaviour for the default verification --------------------

def _captured_default_validator(env, alice, bob):
    _pending_default(env, alice, bob)
    _mock_default_verdict(env, defaulted=False)
    _dispute_default(env, alice, bob)                             # runs (and captures) the validator


@pytest.mark.parametrize("leader,validator_says,agrees", [
    (False, False, True), (True, True, True), (False, True, False), (True, False, False),
])
def test_default_validator_requires_matching_verdict(env, alice, bob, leader, validator_says, agrees):
    _captured_default_validator(env, alice, bob)
    _mock_default_verdict(env, defaulted=validator_says)
    assert env.vm.run_validator(leader_result={"defaulted": leader, "reasoning": "r"}) is agrees


@pytest.mark.parametrize("message,seen,agrees", [
    (DATA_ERR, 404, False), (DATA_ERR, 200, False), (TRANSIENT_ERR, 503, True),
    (TRANSIENT_ERR, 404, False), ("[LLM_ERROR] x", 503, False),
])
def test_default_validator_agrees_on_failures_it_reproduces(env, alice, bob, message, seen, agrees):
    _captured_default_validator(env, alice, bob)
    _mock_default_verdict(env, defaulted=False, status=seen)
    assert env.vm.run_validator(leader_error=Exception(message)) is agrees


# ======================================================================
# evidence-domain whitelist  [#4]
# ======================================================================

@pytest.mark.parametrize("url", [
    "https://evil.example.org/x",
    "https://data.example.com.evil.org/x",       # look-alike suffix
    "https://evil.data.example.com/x",           # subdomain of a listed host is NOT listed
    "https://example.com/x",                      # parent domain
    "https://data.example.com@evil.org/x",        # userinfo trick (also unsafe)
    "https://93.184.216.34/x",                    # public IP, not listed
])
def test_request_rejects_unlisted_domains(env, alice, bob, url):
    env.stake(alice, MIN_STAKE)
    env.mock_score(500)
    env.vm.sender = alice
    with pytest.raises(Exception, match="Domain not whitelisted|Unsafe data_url"):
        env.c.request_score_update(bob, url)
    with pytest.raises(Exception):
        env.c.get_score(bob)


def test_domain_check_is_case_insensitive_and_port_agnostic(env, alice, bob, carol):
    env.stake(alice, MIN_STAKE)
    env.mock_score(500)
    env.vm.sender = alice
    env.c.request_score_update(bob, "https://DATA.Example.COM:8443/path?q=1")
    assert env.c.get_score(bob)["state"] == "PENDING"


def test_challenge_rejects_unlisted_counter_url_but_allows_empty(env, alice, bob, carol, dave):
    env.stake(alice, 300 * GEN)
    env.request(alice, bob, 500)
    env.request(alice, dave, 100)
    with env.vm.expect_revert("Domain not whitelisted"):
        env.challenge(carol, bob, counter_url="https://fake-evidence.example.org/x")
    env.vm.value = 0
    assert env.c.get_score(bob)["state"] == "PENDING"
    assert env.c.get_accounting()["total_escrow"] == 0
    env.challenge(carol, bob, counter_url=COUNTER)                # listed: accepted
    env.challenge(carol, dave)                                    # none: accepted
    assert env.c.get_score(bob)["counter_url"] == COUNTER
    assert env.c.get_score(dave)["counter_url"] == ""
    env.assert_zero_wei([alice])


def test_domain_whitelist_is_governor_only_and_validated(env, carol):
    env.vm.sender = carol
    with env.vm.expect_revert("Only governor"):
        env.c.whitelist_domain("evil.org")
    with env.vm.expect_revert("Only governor"):
        env.c.remove_domain("data.example.com")
    assert env.c.is_domain_allowed("data.example.com")
    assert not env.c.is_domain_allowed("evil.org")
    env.vm.sender = env.governor
    for bad in ("", "  ", "a/b", "host:80", "user@host", "two words", "https://x.org"):
        with env.vm.expect_revert("Invalid domain"):
            env.c.whitelist_domain(bad)
    env.c.whitelist_domain("  NEW.Example.org. ")                  # normalised
    assert env.c.is_domain_allowed("new.example.org")


def test_removed_domain_blocks_new_requests(env, alice, bob):
    env.stake(alice, MIN_STAKE)
    env.vm.sender = env.governor
    env.c.remove_domain("data.example.com")
    assert not env.c.is_domain_allowed("data.example.com")
    env.mock_score(500)
    env.vm.sender = alice
    with env.vm.expect_revert("Domain not whitelisted"):
        env.c.request_score_update(bob, URL)


def test_removing_a_domain_cannot_freeze_an_open_dispute(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 900)
    env.challenge(carol, bob, counter_url=COUNTER)
    env.vm.sender = env.governor
    env.c.remove_domain("data.example.com")
    env.c.remove_domain("counter.example.com")
    env.resolve(bob, 880)                                        # still resolves
    assert env.c.get_score(bob)["state"] == "FINAL"
    env.assert_zero_wei([alice])


# ======================================================================
# loan closure requests  [#5]
# ======================================================================

def _open_loan(env, alice, bob, amount=100 * GEN):
    env.stake(alice, 200 * GEN)
    _lend(env, alice, bob, 800, amount)


def test_request_loan_closure_access_and_state(env, alice, bob, carol):
    _open_loan(env, alice, bob)
    for attacker in (env.rando, carol, bob, env.lender, env.governor):
        env.vm.sender = attacker
        with env.vm.expect_revert("Only the backing evaluator"):
            env.c.request_loan_closure(bob, env.lender)
    env.vm.sender = alice
    with env.vm.expect_revert("No open loan"):
        env.c.request_loan_closure(bob, env.lender2)
    env.c.request_loan_closure(bob, env.lender)
    assert env.c.get_loan(bob, env.lender)["closure_start"] == int(env.t.timestamp())
    with env.vm.expect_revert("Closure already requested"):
        env.c.request_loan_closure(bob, env.lender)


def test_loan_auto_closes_after_the_seven_day_countdown(env, alice, bob):
    _open_loan(env, alice, bob)
    env.vm.sender = alice
    env.c.request_loan_closure(bob, env.lender)
    env.vm.sender = env.rando
    env.advance(CLOSURE_WINDOW - EPS)
    with env.vm.expect_revert("Loan has not expired"):
        env.c.expire_loan(bob, env.lender)
    env.advance(EPS)
    env.c.expire_loan(bob, env.lender)                            # exactly 7 days: anyone may close
    assert env.c.get_loan(bob, env.lender)["amount"] == 0
    assert env.c.get_loan_exposure(alice) == 0
    assert env.c.get_score(bob)["open_loans"] == 0
    assert env.transfers == [] and env.c.get_stake(alice) == 200 * GEN
    with env.vm.expect_revert("No open loan"):
        env.c.expire_loan(bob, env.lender)
    env.vm.sender = env.lender
    with env.vm.expect_revert("No open loan"):
        env.c.report_default(bob, LENDER_EVIDENCE)                                 # too late
    env.assert_exposure_invariant([alice], [bob])
    env.assert_zero_wei([alice])


def test_expire_loan_without_a_request_still_needs_the_ttl(env, alice, bob):
    _open_loan(env, alice, bob)
    env.advance(CLOSURE_WINDOW * 5)
    env.vm.sender = env.rando
    with env.vm.expect_revert("Loan has not expired"):
        env.c.expire_loan(bob, env.lender)                        # nobody asked for closure


def test_lender_can_still_report_default_inside_the_countdown(env, alice, bob):
    _open_loan(env, alice, bob)
    env.vm.sender = alice
    env.c.request_loan_closure(bob, env.lender)
    env.advance(CLOSURE_WINDOW - EPS)
    env.report(bob)                                               # last second: allowed
    loan = env.c.get_loan(bob, env.lender)
    assert loan["status"] == "PENDING_DEFAULT" and loan["closure_start"] == 0
    env.advance(DEFAULT_WINDOW)
    env.vm.sender = env.rando
    with env.vm.expect_revert("Default already reported"):
        env.c.expire_loan(bob, env.lender)                        # the countdown no longer applies
    env.finalize_default(bob)
    assert env.transfers == [(env.lender, 100 * GEN)]
    env.assert_zero_wei([alice])


def test_default_report_after_the_countdown_is_refused(env, alice, bob):
    _open_loan(env, alice, bob)
    env.vm.sender = alice
    env.c.request_loan_closure(bob, env.lender)
    env.advance(CLOSURE_WINDOW)                                   # nobody has called expire_loan yet
    env.vm.sender = env.lender
    with env.vm.expect_revert("Loan closure window elapsed"):
        env.c.report_default(bob, LENDER_EVIDENCE)
    assert env.c.get_loan(bob, env.lender)["amount"] == 100 * GEN  # still pinned until expired


def test_topup_cancels_a_pending_closure_request(env, alice, bob):
    _open_loan(env, alice, bob, amount=50 * GEN)
    env.vm.sender = alice
    env.c.request_loan_closure(bob, env.lender)
    env.record_loan(bob, 10 * GEN)                                # lender re-commits (and pays a fee)
    loan = env.c.get_loan(bob, env.lender)
    assert loan["closure_start"] == 0 and loan["amount"] == 60 * GEN
    env.advance(CLOSURE_WINDOW)
    env.vm.sender = env.rando
    with env.vm.expect_revert("Loan has not expired"):
        env.c.expire_loan(bob, env.lender)


def test_closure_frees_stake_for_unstake(env, alice, bob):
    _open_loan(env, alice, bob, amount=160 * GEN)                 # whole capacity lent
    env.vm.sender = alice
    with env.vm.expect_revert("below exposure"):
        env.c.initiate_unstake(100 * GEN)
    env.c.request_loan_closure(bob, env.lender)
    env.advance(CLOSURE_WINDOW)
    env.vm.sender = env.rando
    env.c.expire_loan(bob, env.lender)
    env.vm.sender = alice
    env.c.initiate_unstake(200 * GEN)                             # nothing pinned any more
    env.assert_zero_wei([alice])


# ======================================================================
# evaluator revenue: fee paid with record_loan  [#6]
# ======================================================================

def test_fee_is_credited_to_the_evaluator_immediately(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    fee = 3 * GEN
    before = env.c.get_accounting()
    env.record_loan(bob, 100 * GEN, fee=fee)
    assert env.c.get_claimable(alice) == fee
    assert env.c.get_claimable(env.lender) == 0
    assert env.c.get_stake(alice) == 200 * GEN                    # not staked
    assert env.c.get_exposure(alice) == 160 * GEN                 # and not exposure
    after = env.c.get_accounting()
    assert after["total_claimable"] == before["total_claimable"] + fee
    assert after["total_deposited"] == before["total_deposited"] + fee
    env.assert_zero_wei([alice])
    assert env.withdraw(alice) == fee                             # withdrawable right away
    assert env.transfers == [(alice, fee)]
    env.assert_zero_wei([alice])


@pytest.mark.parametrize("value,fee,message", [
    (0, FEE, "Sent value must equal fee_amount"),
    (FEE - 1, FEE, "Sent value must equal fee_amount"),
    (FEE + 1, FEE, "Sent value must equal fee_amount"),
    (0, 0, "Fee must be positive"),
    (FEE, 0, "Fee must be positive"),
])
def test_fee_must_be_paid_exactly(env, alice, bob, value, fee, message):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    env.vm.sender = env.lender
    env.vm.value = value
    try:
        with env.vm.expect_revert(message):
            env.c.record_loan(bob, 10 * GEN, fee)
    finally:
        env.vm.value = 0
    assert env.c.get_loan(bob, env.lender)["amount"] == 0
    assert env.c.get_claimable(alice) == 0
    env.assert_zero_wei([alice])


def test_fee_goes_to_the_backing_evaluator_not_the_score_replacer(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.stake(bob, 200 * GEN)
    _lend(env, alice, carol, 800, 160 * GEN, fee=2 * FEE)
    env.request(bob, carol, 700)                                  # fully lent score replaced
    env.advance(timedelta(hours=25))
    env.finalize(carol)
    env.record_loan(carol, 10 * GEN, lender=env.lender2, fee=5 * FEE)
    assert env.c.get_claimable(alice) == 2 * FEE
    assert env.c.get_claimable(bob) == 5 * FEE
    env.assert_zero_wei([alice, bob])


def test_every_topup_pays_a_fee_and_fees_are_never_refunded(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    env.record_loan(bob, 40 * GEN, fee=FEE)
    env.record_loan(bob, 30 * GEN, fee=2 * FEE)
    assert env.c.get_claimable(alice) == 3 * FEE
    env.close_loan(bob)                                            # repaid: fee stays with the evaluator
    assert env.c.get_claimable(alice) == 3 * FEE
    assert env.c.get_claimable(env.lender) == 0
    env.assert_zero_wei([alice])


def test_fee_survives_a_default_and_unstaking(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _lend(env, alice, bob, 800, 100 * GEN, fee=FEE)
    env.default(bob)
    assert env.c.get_stake(alice) == 100 * GEN
    assert env.c.get_claimable(alice) == FEE                       # fees are not slashable stake
    env.assert_zero_wei([alice])


def test_non_lender_cannot_record_a_loan_even_with_a_fee(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    env.vm.sender = carol
    env.vm.value = FEE
    try:
        with env.vm.expect_revert("Only whitelisted lender"):
            env.c.record_loan(bob, 10 * GEN, FEE)
    finally:
        env.vm.value = 0
    assert env.c.get_claimable(alice) == 0


# ======================================================================
# dust-score lockout  [#3]
# ======================================================================

@pytest.mark.min_stake(10 * GEN)
def test_dust_score_can_be_outbid_by_any_evaluator(env, alice, bob, carol):
    env.stake(alice, 10 * GEN)
    env.stake(bob, 100 * GEN)
    _finalize(env, alice, carol, 600)                     # tier 1 on 10 GEN: 5 GEN, dust but > 0
    assert env.c.get_max_borrow_power(carol) == 5 * GEN
    env.request(bob, carol, 600)                          # bob's power 50 GEN > 5 GEN, no loans
    s = env.c.get_score(carol)
    assert s["evaluator"] == bob.as_hex and s["tier"] == 1 and s["state"] == "PENDING"
    assert env.c.get_exposure(alice) == 0                 # alice's dust commitment released
    assert env.c.get_exposure(bob) == 50 * GEN
    env.assert_exposure_invariant([alice, bob], [carol])
    env.assert_zero_wei([alice, bob])


def test_outbid_requires_strictly_greater_borrow_power(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.stake(bob, 200 * GEN)
    _finalize(env, alice, carol, 600)                     # tier 1: power 100 GEN
    for new_score in (0, 100, 499, 500, 699):             # tier 0/1: lower or equal power
        env.mock_score(new_score)
        env.vm.sender = bob
        with env.vm.expect_revert("belongs to another evaluator"):
            env.c.request_score_update(carol, URL)
    assert env.c.get_score(carol)["evaluator"] == alice.as_hex
    env.request(bob, carol, 700)                          # tier 2: 160 GEN, strictly greater
    assert env.c.get_score(carol)["evaluator"] == bob.as_hex


def test_outbid_compares_borrow_power_not_the_raw_score(env, alice, bob, carol):
    env.stake(alice, 1000 * GEN)
    env.stake(bob, MIN_STAKE)
    _finalize(env, alice, carol, 600)                     # tier 1 on a big stake: 500 GEN
    env.mock_score(900)                                   # bob: tier 3 on 100 GEN = 100 GEN < 500
    env.vm.sender = bob
    with env.vm.expect_revert("belongs to another evaluator"):
        env.c.request_score_update(carol, URL)
    assert env.c.get_score(carol)["tier"] == 1


def test_outbid_blocked_once_any_loan_is_open(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.stake(bob, 200 * GEN)
    _lend(env, alice, carol, 600, 1)                       # a 1 wei loan still counts
    assert env.c.get_score(carol)["open_loans"] == 1
    env.mock_score(1000)
    env.vm.sender = bob
    with env.vm.expect_revert("belongs to another evaluator"):
        env.c.request_score_update(carol, URL)
    env.vm.sender = alice
    with env.vm.expect_revert("cannot be replaced before expiry"):
        env.c.request_score_update(carol, URL)                 # nor can the owner re-score
    env.close_loan(carol)                                      # loan repaid -> open_loans back to 0
    assert env.c.get_score(carol)["open_loans"] == 0
    env.request(bob, carol, 1000)                              # now the outbid goes through
    assert env.c.get_score(carol)["evaluator"] == bob.as_hex
    env.assert_exposure_invariant([alice, bob], [carol])
    env.assert_zero_wei([alice, bob])


def test_open_loan_count_tracks_each_lender(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    env.record_loan(bob, 10 * GEN)
    env.record_loan(bob, 10 * GEN)                         # top-up: still one loan
    env.record_loan(bob, 10 * GEN, lender=env.lender2)
    assert env.c.get_score(bob)["open_loans"] == 2
    env.close_loan(bob)
    assert env.c.get_score(bob)["open_loans"] == 1
    env.default(bob, lender=env.lender2)
    assert env.c.get_score(bob)["open_loans"] == 0


def test_cannot_outbid_a_pending_or_disputed_score(env, alice, bob, carol, dave):
    env.stake(alice, 200 * GEN)
    env.stake(bob, 200 * GEN)
    env.request(alice, carol, 1)
    env.mock_score(1000)
    env.vm.sender = bob
    with env.vm.expect_revert("still in challenge process"):
        env.c.request_score_update(carol, URL)
    env.challenge(dave, carol)
    env.vm.sender = bob
    with env.vm.expect_revert("Score is disputed"):
        env.c.request_score_update(carol, URL)


@pytest.mark.min_stake(10 * GEN)
def test_owner_may_raise_its_own_dust_score(env, alice, carol):
    env.stake(alice, 10 * GEN)
    _finalize(env, alice, carol, 600)                     # 5 GEN: dust
    env.request(alice, carol, 900)
    assert env.c.get_score(carol)["tier"] == 3
    assert env.c.get_exposure(alice) == 10 * GEN              # old dust released, new committed
    env.assert_exposure_invariant([alice], [carol])


@pytest.mark.min_stake(10 * GEN)
def test_outbid_still_respects_exposure_cap(env, alice, bob, carol):
    env.stake(alice, 10 * GEN)
    env.stake(bob, 10 * GEN)
    _finalize(env, alice, carol, 600)                          # alice: 5 GEN dust
    env.request(bob, env.many[0], 900)                         # bob already committed all 10 GEN
    env.mock_score(600)                                        # 5 GEN more would exceed bob's stake
    env.vm.sender = bob
    with env.vm.expect_revert("Exposure exceeds stake"):
        env.c.request_score_update(carol, URL)
    assert env.c.get_score(carol)["evaluator"] == alice.as_hex   # alice's dust score survives the revert
    assert env.c.get_exposure(alice) == 5 * GEN
    env.assert_exposure_invariant([alice, bob], [carol, env.many[0]])


# ======================================================================
# single-run dispute resolution (no 3x loop)
# ======================================================================

def test_resolve_dispute_takes_no_median_flag(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)
    env.mock_score(100)
    env.vm.sender = env.rando
    with pytest.raises(Exception):
        env.c.resolve_dispute(bob, True)                       # the old two-argument form is gone
    env.c.resolve_dispute(bob)
    assert env.c.get_score(bob)["tier"] == 0                   # the single run decided
    assert env.c.get_claimable(carol) == 2 * BOND
    env.assert_zero_wei([alice])


def test_single_run_calls_llm_once(env, alice, bob, carol, monkeypatch):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 900)
    env.challenge(carol, bob)
    env.mock_score(900)
    calls = []
    import genlayer as gl

    real = gl.nondet.exec_prompt

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(gl.nondet, "exec_prompt", counting)
    env.vm.sender = env.rando
    env.c.resolve_dispute(bob)
    assert len(calls) == 1


def test_missing_evidence_and_upheld_scores(env, alice, bob, carol, dave):
    env.stake(alice, 1000 * GEN)
    env.request(alice, bob, 500)
    env.request(alice, dave, 600)
    env.challenge(carol, bob)
    env.challenge(carol, dave)
    env.mock_web_status(404)
    env.vm.sender = env.rando
    env.c.resolve_dispute(bob)
    assert env.c.get_score(bob)["score"] == 0
    env.resolve(dave, 380)
    assert env.c.get_score(dave)["tier"] == 1
    env.assert_zero_wei([alice])


# ======================================================================
# two-sided evidence in defaults
# ======================================================================

def test_report_default_stores_lender_evidence(env, alice, bob):
    _pending_default(env, alice, bob)
    assert env.c.get_loan(bob, env.lender)["lender_evidence_url"] == LENDER_EVIDENCE


def test_report_default_requires_safe_whitelisted_lender_evidence(env, alice, bob):
    env.stake(alice, 200 * GEN)
    _lend(env, alice, bob, 800, 100 * GEN)
    env.vm.sender = env.lender
    with env.vm.expect_revert("Unsafe lender_evidence_url"):
        env.c.report_default(bob, "http://127.0.0.1/x")
    with env.vm.expect_revert("Unsafe lender_evidence_url"):
        env.c.report_default(bob, "")
    with env.vm.expect_revert("Domain not whitelisted"):
        env.c.report_default(bob, "https://evil.example.org/claim.json")
    assert env.c.get_loan(bob, env.lender)["status"] == "OPEN"


def test_dispute_prompt_contains_both_sides(env, alice, bob):
    _pending_default(env, alice, bob)
    env.vm.clear_mocks()
    env.vm.mock_web(r".*default-claim\.json.*", {"status": 200, "body": "LENDER-SAYS-MISSED-PAYMENT"})
    env.vm.mock_web(r".*repayment\.json.*", {"status": 200, "body": "EVALUATOR-SHOWS-REPAID"})
    # the mock only answers when the single prompt carries BOTH sources, lender first
    env.vm.mock_llm(
        r"(?s).*impartial judge.*Source L.*LENDER-SAYS-MISSED-PAYMENT.*Source E.*EVALUATOR-SHOWS-REPAID.*",
        json.dumps(json.dumps({"defaulted": False, "reasoning": "repaid"})),
    )
    _dispute_default(env, alice, bob)
    assert env.c.get_loan(bob, env.lender)["status"] == ""        # refuted: cancelled
    assert not env.c.is_lender(env.lender)


def test_lender_evidence_gone_means_claim_unsupported(env, alice, bob):
    _pending_default(env, alice, bob)
    _mock_default_verdict(env, defaulted=True, lender_status=404)  # the LLM is never reached
    _dispute_default(env, alice, bob)
    assert env.transfers == []
    assert env.c.get_stake(alice) == 200 * GEN
    assert not env.c.is_lender(env.lender)
    env.assert_zero_wei([alice])


def test_lender_evidence_transient_error_reverts(env, alice, bob):
    _pending_default(env, alice, bob)
    _mock_default_verdict(env, defaulted=False, lender_status=503)
    env.vm.sender = alice
    with env.vm.expect_revert("TRANSIENT"):
        env.c.dispute_default(bob, env.lender, EVIDENCE)
    assert env.c.get_loan(bob, env.lender)["status"] == "PENDING_DEFAULT"


def test_two_sided_confirmed_default_settles(env, alice, bob):
    _pending_default(env, alice, bob)
    _mock_default_verdict(env, defaulted=True)
    _dispute_default(env, alice, bob)
    assert env.transfers == [(env.lender, 100 * GEN)]
    env.assert_zero_wei([alice])


@pytest.mark.parametrize("lender_status,eval_status,leader,expect", [
    (404, 200, {"defaulted": False}, True),    # lender proof missing -> refuted (validator reproduces)
    (200, 404, {"defaulted": True}, True),     # evaluator proof missing -> default stands
    (404, 200, {"defaulted": True}, False),
    (200, 404, {"defaulted": False}, False),
])
def test_default_validator_reproduces_missing_evidence_outcomes(
    env, alice, bob, lender_status, eval_status, leader, expect
):
    _captured_default_validator(env, alice, bob)
    _mock_default_verdict(env, defaulted=False, status=eval_status, lender_status=lender_status)
    assert env.vm.run_validator(leader_result={**leader, "reasoning": "r"}) is expect


# ======================================================================
# governor timelocks (anti alias-rug)
# ======================================================================

def test_lender_whitelisting_is_timelocked(env, carol):
    env.vm.sender = env.governor
    env.c.propose_whitelist_lender(carol)
    assert env.c.get_pending_lender(carol) == int(env.t.timestamp()) + 3 * 86400
    assert not env.c.is_lender(carol)
    with env.vm.expect_revert("Timelock not elapsed"):
        env.c.execute_whitelist_lender(carol)
    env.advance(TIMELOCK - EPS)
    with env.vm.expect_revert("Timelock not elapsed"):
        env.c.execute_whitelist_lender(carol)
    env.advance(EPS)
    env.c.execute_whitelist_lender(carol)
    assert env.c.is_lender(carol)
    assert env.c.get_pending_lender(carol) == 0
    with env.vm.expect_revert("Nothing proposed"):
        env.c.execute_whitelist_lender(carol)           # no replay


def test_lender_timelock_rejections_and_cancel(env, alice, carol):
    env.vm.sender = env.governor
    with env.vm.expect_revert("Nothing proposed"):
        env.c.execute_whitelist_lender(carol)
    env.c.propose_whitelist_lender(carol)
    with env.vm.expect_revert("Already proposed"):
        env.c.propose_whitelist_lender(carol)
    env.vm.sender = alice
    with env.vm.expect_revert("Only governor"):
        env.c.cancel_whitelist_lender(carol)
    env.vm.sender = env.governor
    env.c.cancel_whitelist_lender(carol)
    env.advance(TIMELOCK)
    with env.vm.expect_revert("Nothing proposed"):
        env.c.execute_whitelist_lender(carol)
    assert not env.c.is_lender(carol)


def test_alias_lender_is_not_usable_during_the_timelock(env, alice, bob):
    """A rogue governor's alias cannot record loans or report defaults until the delay
    passes -- the window evaluators have to exit."""
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, 800)
    alias = env.many[6]
    env.vm.sender = env.governor
    env.c.propose_whitelist_lender(alias)
    with env.vm.expect_revert("Only whitelisted lender"):
        env.record_loan(bob, 10 * GEN, lender=alias)
    env.vm.sender = alias
    with env.vm.expect_revert("No open loan"):
        env.c.report_default(bob, LENDER_EVIDENCE)
    # alice sees the proposal and starts unbonding inside the window
    _unstake(env, alice, 40 * GEN)
    env.assert_zero_wei([alice])


def test_remove_lender_stays_instant(env):
    env.vm.sender = env.governor
    env.c.remove_lender(env.lender)
    assert not env.c.is_lender(env.lender)


def test_treasury_payout_is_timelocked(env, alice, bob, carol):
    _fund_treasury(env, alice, bob, carol)
    env.vm.sender = env.governor
    with env.vm.expect_revert("Nothing proposed"):
        env.c.execute_claim_treasury()
    env.c.propose_claim_treasury(3 * GEN, env.many[0])
    p = env.c.get_pending_treasury()
    assert p["amount"] == 3 * GEN and p["destination"] == env.many[0].as_hex
    assert p["eta"] == int(env.t.timestamp()) + 3 * 86400
    with env.vm.expect_revert("Payout already proposed"):
        env.c.propose_claim_treasury(1, env.many[1])
    with env.vm.expect_revert("Timelock not elapsed"):
        env.c.execute_claim_treasury()
    env.advance(TIMELOCK - EPS)
    with env.vm.expect_revert("Timelock not elapsed"):
        env.c.execute_claim_treasury()
    assert env.transfers == [] and env.c.get_treasury() == BOND
    env.advance(EPS)
    env.c.execute_claim_treasury()
    assert env.transfers == [(env.many[0], 3 * GEN)]
    assert env.c.get_treasury() == BOND - 3 * GEN
    assert env.c.get_pending_treasury()["eta"] == 0
    with env.vm.expect_revert("Nothing proposed"):
        env.c.execute_claim_treasury()
    env.assert_zero_wei([alice])


def test_treasury_proposal_access_and_cancel(env, alice, bob, carol):
    _fund_treasury(env, alice, bob, carol)
    env.vm.sender = env.rando
    for call in (lambda: env.c.execute_claim_treasury(), lambda: env.c.cancel_claim_treasury()):
        with env.vm.expect_revert("Only governor"):
            call()
    env.vm.sender = env.governor
    env.c.propose_claim_treasury(BOND, env.many[0])
    env.c.cancel_claim_treasury()
    env.advance(TIMELOCK)
    with env.vm.expect_revert("Nothing proposed"):
        env.c.execute_claim_treasury()
    assert env.transfers == []
    env.c.propose_claim_treasury(BOND, env.many[0])          # a fresh proposal after a cancel
    env.assert_zero_wei([alice])


def test_old_instant_governor_entry_points_are_gone(env):
    env.vm.sender = env.governor
    with pytest.raises(Exception):
        env.c.whitelist_lender(env.many[0])
    with pytest.raises(Exception):
        env.c.claim_treasury(1, env.many[0])


# ======================================================================
# dust-score squatting prevention (< 10 GEN, no loans)
# ======================================================================

@pytest.mark.min_stake(10 * GEN)
def test_dust_score_is_overwritten_by_any_evaluator_even_with_lower_power(env, alice, bob, carol):
    env.stake(alice, 10 * GEN)
    env.stake(bob, 10 * GEN)
    _finalize(env, alice, carol, 800)                     # tier 2 on 10 GEN: 8 GEN < 10 GEN: dust
    env.request(bob, carol, 600)                          # LOWER power (5 GEN): still allowed
    assert env.c.get_score(carol)["evaluator"] == bob.as_hex
    assert env.c.get_exposure(alice) == 0
    env.assert_exposure_invariant([alice, bob], [carol])
    env.assert_zero_wei([alice, bob])


@pytest.mark.min_stake(10 * GEN)
def test_score_at_the_dust_threshold_is_still_locked(env, alice, bob, carol):
    env.stake(alice, 10 * GEN)
    env.stake(bob, 10 * GEN)
    _finalize(env, alice, carol, 900)                     # tier 3 on 10 GEN: exactly 10 GEN: not dust
    env.mock_score(600)
    env.vm.sender = bob
    with env.vm.expect_revert("belongs to another evaluator"):
        env.c.request_score_update(carol, URL)
    assert env.c.get_score(carol)["evaluator"] == alice.as_hex


@pytest.mark.min_stake(10 * GEN)
def test_dust_score_with_an_open_loan_is_protected(env, alice, bob, carol):
    env.stake(alice, 10 * GEN)
    env.stake(bob, 10 * GEN)
    _lend(env, alice, carol, 600, 1)                      # dust, but a loan is open
    env.mock_score(900)                                   # even strictly greater power is refused
    env.vm.sender = bob
    with env.vm.expect_revert("belongs to another evaluator"):
        env.c.request_score_update(carol, URL)
    assert env.c.get_loan(carol, env.lender)["amount"] == 1


@pytest.mark.min_stake(10 * GEN)
def test_pending_or_disputed_dust_score_is_still_protected(env, alice, bob, carol, dave):
    env.stake(alice, 10 * GEN)
    env.stake(bob, 10 * GEN)
    env.request(alice, carol, 600)                        # PENDING, inside the challenge window
    env.mock_score(900)
    env.vm.sender = bob
    with env.vm.expect_revert("still in challenge process"):
        env.c.request_score_update(carol, URL)
    env.challenge(dave, carol)
    env.vm.sender = bob
    with env.vm.expect_revert("Score is disputed"):
        env.c.request_score_update(carol, URL)


# ======================================================================
# end-to-end zero-wei
# ======================================================================

def test_full_lifecycle_zero_wei(env, alice, bob, carol, dave):
    env.stake(alice, 300 * GEN)
    env.stake(bob, 123 * GEN + 7)
    env.request(alice, carol, 900)                        # tier 3: 300
    env.request(bob, dave, 600)                           # tier 1
    env.assert_zero_wei([alice, bob])

    env.challenge(bob, carol, counter_url=COUNTER)        # overturned
    env.challenge(alice, dave)                            # upheld
    env.assert_zero_wei([alice, bob])
    env.resolve(carol, 500)
    env.resolve(dave, 450)
    env.assert_zero_wei([alice, bob])
    env.assert_exposure_invariant([alice, bob], [carol, dave])

    env.advance(timedelta(hours=1))
    env.record_loan(carol, 50 * GEN)
    env.record_loan(carol, 40 * GEN, lender=env.lender2)
    env.record_loan(dave, 5)
    env.assert_exposure_invariant([alice, bob], [carol, dave])
    env.default(carol)
    env.default(carol, lender=env.lender2)
    env.default(dave)
    env.assert_zero_wei([alice, bob])
    env.assert_exposure_invariant([alice, bob], [carol, dave])
    assert env.c.get_loan_exposure(alice) == 0 and env.c.get_loan_exposure(bob) == 0

    for who in (alice, bob):
        if env.c.get_claimable(who):
            env.withdraw(who)
    env.assert_zero_wei([alice, bob])
    assert env.c.get_accounting()["total_claimable"] == 0


def test_everything_zero_wei(env, alice, bob, carol, dave):
    env.stake(alice, 300 * GEN)
    env.stake(bob, 120 * GEN)
    env.request(alice, carol, 800)                        # tier 2: capacity 240
    _unstake(env, alice, 50 * GEN)
    env.challenge(bob, carol)
    env.assert_zero_wei([alice, bob])
    env.resolve(carol, 300)                               # tier 0: overturned
    env.request(bob, dave, 600)                           # tier 1: capacity 60
    _unstake(env, bob, 60 * GEN)
    env.challenge(alice, dave, counter_url=COUNTER)
    env.resolve(dave, 320)                                # upheld
    env.assert_zero_wei([alice, bob])
    env.warp(timedelta(days=2))
    env.record_loan(dave, 2 * GEN)                        # live power: tier 1 on 60 GEN = 30 GEN
    env.default(dave)
    env.assert_zero_wei([alice, bob])
    env.claim_treasury(env.c.get_treasury() // 2, env.rando)
    env.assert_zero_wei([alice, bob])
    env.warp(timedelta(days=40))
    for who in (alice, bob):
        if env.c.get_unbonding(who)["amount"]:
            _claim_unstaked(env, who)
        env.assert_zero_wei([alice, bob])
    for who in (alice, bob, carol):
        if env.c.get_claimable(who):
            env.withdraw(who)
        env.assert_zero_wei([alice, bob])
    assert env.c.get_accounting()["total_claimable"] == 0


# ======================================================================
# deterministic economic consensus: agreed buckets (steward finding)
#
# Direct mode runs the leader once; `run_validator` then executes the REAL
# validator_fn against (a) the validator's own mocked world and (b) a leader
# output we forge. This is exactly the check a GenLayer validator performs.
# ======================================================================

FLOORS = [0, 500, 700, 850]          # independent reference copy of the bucket table
CAP_BPS = [0, 5000, 8000, 10000]
OLD_TOLERANCE = 100                  # the retired +/-100 score tolerance


def ref_tier(score):
    return max(i for i, f in enumerate(FLOORS) if score >= f)


def ref_capacity(score, stake):
    return CAP_BPS[ref_tier(score)] * stake // 10000


def _leader_payload(tier):
    return {"tier": tier, "reasoning": "forged"}


def test_tier_table_is_contiguous_and_matches_reference(env):
    t = env.c.get_tier_table()
    assert t["floors"] == FLOORS and t["capacity_bps"] == CAP_BPS
    assert t["floors"][0] == 0 and t["floors"] == sorted(set(t["floors"]))
    assert t["capacity_bps"] == sorted(t["capacity_bps"]) and t["capacity_bps"][0] == 0
    assert t["max_score"] == 1000 and t["overturn_tier_gap"] == OVERTURN_TIER_GAP
    for score in range(0, 1001):                      # every score maps to exactly one tier
        assert env.c.get_tier_for_score(score) == ref_tier(score)
    with env.vm.expect_revert("out of range"):
        env.c.get_tier_for_score(1001)
    with env.vm.expect_revert("out of range"):
        env.c.get_tier_for_score(-1)


def test_borderline_leader_cannot_push_borrower_into_higher_tier(env, alice, bob):
    """Leader claims 700 (tier 2, 80%); an honest validator computes 699 (tier 1, 50%).
    The scores differ by 1 -- the retired +/-100 tolerance accepted this and the
    capacity differed by 30% of the stake. Exact bucket equality rejects it."""
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 700)                               # leader's run: tier 2
    assert env.c.get_score(bob)["exposure"] == 160 * GEN
    assert abs(700 - 699) <= OLD_TOLERANCE                     # old rule would have passed

    env.mock_score(699)                                        # validator's own evaluation
    assert env.vm.run_validator() is False                     # stored leader output: tier 2
    assert env.vm.run_validator(leader_result=_leader_payload(2)) is False
    assert env.vm.run_validator(leader_result=_leader_payload(1)) is True


@pytest.mark.parametrize("validator_score,forged_tier", [
    (0, 1), (499, 1), (500, 2), (699, 2), (700, 3), (849, 3),    # one tier up, all borderline
    (1000, 2), (850, 1), (700, 0),                               # downward / multi-tier forgery
])
def test_validator_rejects_any_leader_tier_other_than_its_own(env, alice, bob, validator_score, forged_tier):
    _captured_validator(env, alice, bob)
    env.mock_score(validator_score)
    assert forged_tier != ref_tier(validator_score)
    assert env.vm.run_validator(leader_result=_leader_payload(forged_tier)) is False
    assert env.vm.run_validator(leader_result=_leader_payload(ref_tier(validator_score))) is True


def test_acceptance_is_exactly_bucket_equality_for_every_score(env, alice, bob):
    """For all 1001 validator scores and all 4 leader tiers: accept <=> same bucket."""
    _captured_validator(env, alice, bob)
    for v in range(0, 1001):
        env.mock_score(v)
        for t in range(4):
            expected = (t == ref_tier(v))
            assert env.vm.run_validator(leader_result=_leader_payload(t)) is expected, (v, t)


@pytest.mark.parametrize("bad", [
    {"score": 700, "reasoning": "r"},           # legacy payload: no tier -> never trusted
    {"tier": 4, "reasoning": "r"},              # out of range
    {"tier": -1, "reasoning": "r"},
    {"tier": "2", "reasoning": "r"},            # wrong type
    {"tier": 2.0, "reasoning": "r"},
    {"tier": True, "reasoning": "r"},           # bool is not a tier
    {"tier": None, "reasoning": "r"},
    {},
    [],
    "tier",
    None,
])
def test_malformed_leader_payloads_are_rejected(env, alice, bob, bad):
    _captured_validator(env, alice, bob)
    env.mock_score(700)
    assert env.vm.run_validator(leader_result=bad) is False


def test_extra_leader_fields_cannot_move_capacity(env, alice, bob):
    """A leader smuggling a continuous `score` next to an agreed tier: the validator
    compares tiers only, and the contract stores only the tier (score is the tier floor)."""
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 720)                               # tier 2
    env.mock_score(710)                                        # validator: tier 2
    assert env.vm.run_validator(leader_result={"tier": 2, "score": 849, "reasoning": "r"}) is True
    s = env.c.get_score(bob)
    assert s["tier"] == 2 and s["score"] == 700                # canonical floor, not 720
    assert s["exposure"] == ref_capacity(720, 200 * GEN)


def test_every_validator_compatible_output_yields_identical_capacity(env, alice, bob, carol):
    """Take every leader score; every validator that accepts it must have the same
    bucket, hence the same capacity. Across all 1001 scores the capacity takes exactly
    4 distinct values and is constant inside each bucket."""
    stake = 200 * GEN
    env.stake(alice, stake)
    by_tier = {0: set(), 1: set(), 2: set(), 3: set()}
    snap = env.vm.snapshot()
    for leader_score in range(0, 1001, 7):
        env.request(alice, bob, leader_score)
        cap = env.c.get_score(bob)["exposure"]
        tier = env.c.get_score(bob)["tier"]
        assert tier == ref_tier(leader_score) and cap == ref_capacity(leader_score, stake)
        # validators whose own score is anywhere in the SAME bucket accept...
        lo = FLOORS[tier]
        hi = (FLOORS[tier + 1] - 1) if tier < 3 else 1000
        for v in (lo, (lo + hi) // 2, hi):
            env.mock_score(v)
            assert env.vm.run_validator() is True
        # ...and every other bucket's validator rejects, however close the score is.
        for v in (lo - 1, hi + 1):
            if 0 <= v <= 1000:
                env.mock_score(v)
                assert env.vm.run_validator() is False
        by_tier[tier].add(cap)
        env.vm.revert(snap)
        snap = env.vm.snapshot()
    assert all(len(caps) == 1 for caps in by_tier.values())
    assert sorted(next(iter(c)) for c in by_tier.values()) == [0, 100 * GEN, 160 * GEN, 200 * GEN]


def test_zero_economic_drift_across_independent_validators(env, alice, bob):
    """Five independent validators see differently-noisy scores for the same evidence.
    Under the old rule a leader at 760 was accepted by validators at 660..860, spanning
    tiers 1..3 (50%..100% of stake). Now only the validators in the leader's bucket
    accept, and they all apply the identical capacity."""
    stake = 200 * GEN
    env.stake(alice, stake)
    env.request(alice, bob, 760)                               # leader: tier 2
    leader = env.vm.run_validator                              # stored leader output (tier 2)

    old_accepting = [v for v in range(0, 1001) if abs(v - 760) <= OLD_TOLERANCE]
    assert {ref_tier(v) for v in old_accepting} == {1, 2, 3}   # the retired rule's drift
    assert len({ref_capacity(v, stake) for v in old_accepting}) == 3

    accepting = []
    for v in range(0, 1001):
        env.mock_score(v)
        if leader() is True:
            accepting.append(v)
    assert accepting == list(range(700, 850))                  # exactly the bucket
    assert {ref_capacity(v, stake) for v in accepting} == {ref_capacity(760, stake)}
    assert env.c.get_score(bob)["exposure"] == 160 * GEN


@pytest.mark.parametrize("score,tier,cap_gen", [
    (0, 0, 0), (499, 0, 0), (500, 1, 100), (699, 1, 100),
    (700, 2, 160), (849, 2, 160), (850, 3, 200), (1000, 3, 200),
])
def test_borrow_capacity_is_bound_to_bucket_at_every_boundary(env, alice, bob, score, tier, cap_gen):
    env.stake(alice, 200 * GEN)
    _finalize(env, alice, bob, score)
    s = env.c.get_score(bob)
    assert s["tier"] == tier and s["score"] == FLOORS[tier] and s["capacity_bps"] == CAP_BPS[tier]
    assert env.c.get_max_borrow_power(bob) == cap_gen * GEN
    if cap_gen:
        env.record_loan(bob, cap_gen * GEN)
        with env.vm.expect_revert("exceeds borrow power"):
            env.record_loan(bob, 1, lender=env.lender2)
    else:
        with env.vm.expect_revert("exceeds borrow power"):
            env.record_loan(bob, 1)
    env.assert_zero_wei([alice])


def test_capacity_does_not_depend_on_unagreed_score_variation(env, alice, bob, carol):
    """Same evidence, two honest runs at different raw scores in one bucket: the
    resulting records are identical in every economically relevant field."""
    env.stake(alice, 200 * GEN)
    env.stake(env.many[0], 200 * GEN)
    _finalize(env, alice, bob, 705)
    _finalize(env, env.many[0], carol, 845)
    a, c = env.c.get_score(bob), env.c.get_score(carol)
    for field in ("tier", "score", "capacity_bps", "exposure"):
        assert a[field] == c[field]
    assert env.c.get_max_borrow_power(bob) == env.c.get_max_borrow_power(carol) == 160 * GEN


def test_dispute_noise_across_a_boundary_never_slashes_but_two_tiers_does(env, alice, bob, carol):
    env.stake(alice, 200 * GEN)
    env.request(alice, bob, 500)                               # tier 1
    env.challenge(carol, bob)
    env.resolve(bob, 499)                                      # adjacent bucket: honest noise
    assert env.c.get_score(bob)["tier"] == 1 and env.c.get_claimable(carol) == 0

    env.request(alice, env.many[0], 500)
    env.challenge(carol, env.many[0])
    env.resolve(env.many[0], 850)                              # two buckets away: overturned
    assert env.c.get_score(env.many[0])["tier"] == 3
    assert env.c.get_claimable(carol) == 2 * BOND
    env.assert_zero_wei([alice])
