"""Just enough of a browser to run the shipped scripts under `dukpy`.

The scripts are executed as they ship, so a test observes what a visitor's
browser would do -- which layout lands on the page, which key a preference is
stored under -- rather than whether a particular line is still in the source.
Only the DOM surface the scripts touch is faked.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import dukpy

STATIC = Path(__file__).resolve().parent.parent / "src/nineveh/static"

DOM = r"""
function FakeElement(options) {
  options = options || {};
  this.dataset = Object.assign({}, options.dataset || {});
  this.attributes = {};
  this.listeners = {};
  this.children = options.children || {};
  this.anyChild = Boolean(options.anyChild);
  this.textContent = "";
  this.hidden = false;
  this.disabled = false;
  this.classList = { toggle() {}, add() {}, remove() {} };
}
FakeElement.prototype.setAttribute = function (name, value) {
  this.attributes[name] = String(value);
};
FakeElement.prototype.getAttribute = function (name) {
  return name in this.attributes ? this.attributes[name] : null;
};
FakeElement.prototype.addEventListener = function (type, listener) {
  (this.listeners[type] = this.listeners[type] || []).push(listener);
};
FakeElement.prototype.dispatch = function (type) {
  (this.listeners[type] || []).forEach(function (listener) { listener({}); });
};
FakeElement.prototype.querySelector = function (selector) {
  if (!(selector in this.children) && this.anyChild) {
    this.children[selector] = new FakeElement();
  }
  const found = this.children[selector];
  return Array.isArray(found) ? found[0] : found || null;
};
FakeElement.prototype.querySelectorAll = function (selector) {
  const found = this.children[selector];
  return found ? [].concat(found) : [];
};

function FakeStorage(items, broken) {
  this.items = Object.assign({}, items || {});
  this.broken = Boolean(broken);
}
FakeStorage.prototype.getItem = function (key) {
  if (this.broken) throw new Error("storage is disabled");
  return key in this.items ? this.items[key] : null;
};
FakeStorage.prototype.setItem = function (key, value) {
  if (this.broken) throw new Error("storage is disabled");
  this.items[key] = String(value);
};

var document = new FakeElement();
document.documentElement = new FakeElement();
document.body = new FakeElement();
var window = new FakeElement();
window.localStorage = new FakeStorage(dukpy.storage, dukpy.brokenStorage);
window.matchMedia = function () { return { matches: false, addEventListener() {} }; };
window.setTimeout = function () { return 0; };
window.clearTimeout = function () {};
window.location = { href: "https://nineveh.test/", assign() {} };
"""


class FakeBrowser:
    """One page load: a fresh engine, DOM, and `localStorage`."""

    def __init__(
        self, storage: dict[str, str] | None = None, *, broken_storage: bool = False
    ):
        self._engine = dukpy.JSInterpreter()
        self._engine.evaljs(DOM, storage=storage or {}, brokenStorage=broken_storage)

    def run(self, source: str, **names: object):
        """Evaluate `source`; promise jobs it starts settle before the next call."""
        return self._engine.evaljs(source, **names)

    def load(self, script: str) -> None:
        self.run((STATIC / script).read_text(encoding="utf-8"))

    def storage(self) -> dict[str, str]:
        return self.run("window.localStorage.items")

    def stored(self, key: str):
        value = self.storage().get(key)
        return None if value is None else json.loads(value)


def without_module_syntax(source: str) -> str:
    """ES imports and exports stripped, for an engine that runs plain scripts."""
    imports = re.compile(r"^import \{[^}]*\} from \"[^\"]+\";\n", re.MULTILINE)
    exports = re.compile(r"^export \{[^}]*\};\n", re.MULTILINE)
    source = exports.sub("", imports.sub("", source))
    return source.replace("export function ", "function ").replace(
        "export const ", "const "
    )
