def test_jetblue_scrape_imports_and_configures(monkeypatch):
    monkeypatch.setenv("MOTHERDUCK_TOKEN", "dummy")
    import jetblue_scrape
    assert jetblue_scrape.MAX_LEGS_PER_SHARD == 30
    from scrapers.jetblue import JetBlueScraper
    assert JetBlueScraper.airline_code == "B6"
