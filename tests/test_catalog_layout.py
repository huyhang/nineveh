"""The catalog layout switcher, run as the browser runs it."""

from __future__ import annotations

import pytest
from fake_browser import FakeBrowser

KEY = "nineveh-catalog-layout"
BUTTONS = """
var buttons = ["default", "dense", "list"].map(function (layout) {
  return new FakeElement({ dataset: { layoutOption: layout } });
});
document.children["[data-layout-option]"] = buttons;
"""


def _page(storage: dict[str, str] | None = None, **options) -> FakeBrowser:
    browser = FakeBrowser(storage, **options)
    browser.run(BUTTONS)
    browser.load("catalog-layout.js")
    return browser


def _layout(browser: FakeBrowser) -> str:
    return browser.run("document.documentElement.dataset.catalogLayout")


def _pressed(browser: FakeBrowser) -> list[str]:
    return browser.run(
        "buttons.filter(function (button) {"
        "  return button.getAttribute('aria-pressed') === 'true';"
        "}).map(function (button) { return button.dataset.layoutOption; })"
    )


@pytest.mark.parametrize("layout", ["default", "dense", "list"])
def test_the_remembered_layout_is_on_the_page_before_it_is_parsed(layout: str):
    """Applied to <html> at once, so the grid never paints the default first."""
    browser = _page({KEY: layout})

    assert _layout(browser) == layout
    browser.run("document.dispatch('DOMContentLoaded')")
    assert _pressed(browser) == [layout]


@pytest.mark.parametrize("stored", [None, "", "tiles", "3"])
def test_an_unknown_or_missing_layout_falls_back_to_default(stored: str | None):
    browser = _page({} if stored is None else {KEY: stored})

    assert _layout(browser) == "default"


def test_choosing_a_layout_applies_it_and_is_remembered_by_the_next_page():
    browser = _page()
    browser.run("document.dispatch('DOMContentLoaded'); buttons[2].dispatch('click')")

    assert _layout(browser) == "list"
    assert _pressed(browser) == ["list"]
    assert browser.storage() == {KEY: "list"}
    assert _layout(_page(browser.storage())) == "list"


def test_a_browser_without_storage_still_switches_layout_for_the_page():
    browser = _page(broken_storage=True)
    assert _layout(browser) == "default"

    browser.run("document.dispatch('DOMContentLoaded'); buttons[1].dispatch('click')")

    assert _layout(browser) == "dense"
    assert _pressed(browser) == ["dense"]
