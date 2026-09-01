import assert from "node:assert/strict";
import test from "node:test";

import {
  addSourceUi,
  createSourceUi,
  markSourcesSaved,
  moveSourceUi,
  removeSourceUi,
} from "../pages/settings/source-ui-state.mjs";

test("stable ids preserve multiple expanded cards across list edits and moves", () => {
  const sources = [{ apis: ["a"] }, { apis: ["b"] }];
  const ui = createSourceUi(sources);
  const [firstId, secondId] = ui.ids;
  ui.openIds.add(secondId);

  sources[0].apis.push("c");
  sources[0].apis.splice(0, 1);

  moveSourceUi(ui, 0, 1);

  assert.deepEqual(ui.ids, [secondId, firstId]);
  assert.deepEqual([...ui.openIds].sort(), [firstId, secondId].sort());
});

test("new source is marked new and open until a successful save", () => {
  const ui = createSourceUi([{}]);

  const newId = addSourceUi(ui);

  assert.equal(ui.ids.at(-1), newId);
  assert.equal(ui.newIds.has(newId), true);
  assert.equal(ui.openIds.has(newId), true);
  markSourcesSaved(ui);
  assert.equal(ui.newIds.size, 0);
});

test("removal drops UI metadata and opens the adjacent source", () => {
  const ui = createSourceUi([{}, {}, {}]);
  const removedId = ui.ids[1];
  ui.newIds.add(removedId);
  ui.openIds.clear();

  const adjacentId = removeSourceUi(ui, 1);

  assert.equal(ui.ids.includes(removedId), false);
  assert.equal(ui.newIds.has(removedId), false);
  assert.equal(ui.openIds.has(removedId), false);
  assert.equal(adjacentId, ui.ids[1]);
  assert.equal(ui.openIds.has(adjacentId), true);
});
