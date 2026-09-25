"""The theme switcher, run as the browser runs it."""

from __future__ import annotations

from fake_browser import FakeBrowser

TOGGLES = """
function toggle() {
  return new FakeElement({ children: {
    ".theme-icon": new FakeElement(),
    "[data-theme-label]": new FakeElement(),
  } });
}
// The header and the mobile menu each carry one.
var toggles = [toggle(), toggle()];
document.children["[data-theme-toggle]"] = toggles;
"""
LABELS = (
    "toggles.map(function (button) {"
    "  return button.querySelector('[data-theme-label]').textContent;"
    "})"
)


def _page(storage: dict[str, str] | None = None) -> FakeBrowser:
    browser = FakeBrowser(storage)
    browser.run(TOGGLES)
    browser.load("theme.js")
    browser.run("document.dispatch('DOMContentLoaded')")
    return browser


def test_every_visible_theme_control_shows_the_remembered_theme():
    browser = _page({"nineveh-theme": "paper"})

    assert browser.run("document.documentElement.dataset.theme") == "paper"
    assert browser.run(LABELS) == ["Paper", "Paper"]


def test_switching_from_one_control_updates_every_other():
    browser = _page({"nineveh-theme": "dark"})

    browser.run("toggles[1].dispatch('click')")

    assert browser.run("document.documentElement.dataset.theme") == "system"
    assert browser.run(LABELS) == ["System", "System"]
    assert browser.storage() == {"nineveh-theme": "system"}
