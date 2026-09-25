"""The browser reader's controller, run under stubs as the browser runs it.

Rendering is stubbed out; what is exercised is where the reader keeps its
reading mode and what it sends to the account.
"""

from __future__ import annotations

import json

from fake_browser import STATIC, FakeBrowser, without_module_syntax

HARNESS = """
var requests = [];
function recordingFetch(url, options) {
  requests.push({ url: url, options: options });
  return Promise.resolve({
    ok: true,
    status: 200,
    json: function () { return Promise.resolve({ updatedAt: "2026-01-01T00:00:00Z" }); },
  });
}
var readers = [];
function openReader(dataset) {
  const root = new FakeElement({ anyChild: true, dataset: Object.assign({
    userId: "reader-1",
    seriesId: "series-1",
    publicationId: "volume-1",
    csrfToken: "token",
    pageCount: "12",
    initialPage: "1",
    initialMode: "single",
    explicitMode: "false",
    explicitPage: "false",
    completed: "false",
    progressUpdatedAt: "0",
  }, dataset) });
  const reader = new ReaderController(root, {
    fetch: recordingFetch,
    storage: window.localStorage,
    history: { replaceState() {} },
  });
  reader.render = async function () {};
  readers.push(reader);
  return reader;
}
"""


def _browser() -> FakeBrowser:
    browser = FakeBrowser()
    model = without_module_syntax((STATIC / "reader-model.js").read_text("utf-8"))
    reader = without_module_syntax((STATIC / "reader.js").read_text("utf-8"))
    # `reader.js` starts itself on a page with `[data-reader]`; this one has none.
    browser.run(f"{model}\n{reader}\n{HARNESS}")
    return browser


def _mode(browser: FakeBrowser, **dataset: str) -> str:
    return browser.run("openReader(dukpy.dataset).state.mode", dataset=dataset)


def test_choosing_a_mode_is_remembered_for_that_series_on_this_device():
    browser = _browser()
    browser.run("openReader({}).setMode('double')")

    assert browser.stored("nineveh-reader-mode:reader-1:series-1") == {"mode": "double"}
    assert _mode(browser, publicationId="volume-2") == "double"


def test_another_series_or_another_account_does_not_inherit_the_mode():
    browser = _browser()
    browser.run("openReader({}).setMode('scroll')")

    assert _mode(browser, seriesId="series-2") == "single"
    assert _mode(browser, userId="reader-2") == "single"


def test_a_link_that_names_a_mode_wins_over_the_remembered_one():
    browser = _browser()
    browser.run("openReader({}).setMode('double')")

    assert _mode(browser, initialMode="scroll", explicitMode="true") == "scroll"


def test_progress_sent_to_the_account_carries_no_reading_mode():
    """The account-wide mode belongs to older apps; the browser keeps its own."""
    browser = _browser()
    browser.run(
        "const reader = openReader({ initialMode: 'double', explicitMode: 'true' });"
        "reader.state.page = 4;"
        "reader.saveRemote();"
    )

    [request] = browser.run("requests")
    assert request["url"] == "/api/v1/publications/volume-1/progress"
    assert request["options"]["method"] == "PUT"
    assert request["options"]["headers"]["X-CSRF-Token"] == "token"
    assert json.loads(request["options"]["body"]) == {"page": 4, "completed": False}
