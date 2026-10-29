"""Qt tests never reach the internet for the Armenian model's list (tests_qt/conftest.py)."""


def test_a_test_never_asks_the_website_for_the_model_list():
    import speech_hy
    assert speech_hy.fetch_manifest() is None                 # the fixture answers "no entry", no request
    speech_hy.refresh_catalog(force=True)                     # what a page does when it opens
    assert not speech_hy.listed()                             # so the model stays out of every list


def test_a_page_that_opens_through_the_real_downloader_stays_offline(qapp):
    # The real ModelDownloads (not a stand-in) asks for the list on a worker thread.
    import time
    import model_manager
    import speech_hy
    dl = model_manager.ModelDownloads()
    dl.refresh_catalog(force=True)
    end = time.time() + 3
    while time.time() < end:
        qapp.processEvents()
        time.sleep(0.02)
    assert not speech_hy.listed()
