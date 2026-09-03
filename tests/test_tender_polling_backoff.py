from pathlib import Path


def test_tender_board_pauses_and_backs_off_live_status_polling():
    template = (
        Path(__file__).parents[1] / "autobot" / "templates" / "tenders.html"
    ).read_text(encoding="utf-8")

    assert 'document.addEventListener("visibilitychange"' in template
    assert "if (document.hidden)" in template
    assert "Math.min(30000" in template
    assert "scheduleLiveStatusPoll(active ? 1800 : idleDelay)" in template
    assert "setInterval(refreshLiveStatus, 1800)" not in template
