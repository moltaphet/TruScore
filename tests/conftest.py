def pytest_configure(config):
    config.addinivalue_line("markers", "min_stake(amount): deploy the oracle with this minimum stake")
