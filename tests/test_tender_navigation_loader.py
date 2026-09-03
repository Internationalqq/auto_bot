from pathlib import Path


def test_tender_board_shows_loader_before_opening_a_tender():
    package_dir = Path(__file__).parents[1] / "autobot"
    template = (package_dir / "templates" / "tenders.html").read_text(encoding="utf-8")
    styles = (package_dir / "static" / "tenders.css").read_text(encoding="utf-8")

    assert 'id="tenderNavigationLoader"' in template
    assert "function showTenderNavigationLoader()" in template
    assert 'showTenderNavigationLoader();\n      window.location.href = card.getAttribute("data-href");' in template
    assert ".tender-navigation-loader {" in styles
    assert "@keyframes tender-navigation-loading" in styles
