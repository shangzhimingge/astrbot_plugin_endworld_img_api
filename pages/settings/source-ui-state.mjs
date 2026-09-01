function nextSourceId(ui) {
  const id = `source-ui-${ui.nextId}`;
  ui.nextId += 1;
  return id;
}

export function createSourceUi(sources) {
  const ui = {
    ids: [],
    openIds: new Set(),
    newIds: new Set(),
    nextId: 1,
  };
  ui.ids = sources.map(() => nextSourceId(ui));
  if (ui.ids[0]) ui.openIds.add(ui.ids[0]);
  return ui;
}

export function addSourceUi(ui) {
  const id = nextSourceId(ui);
  ui.ids.push(id);
  ui.newIds.add(id);
  ui.openIds.add(id);
  return id;
}

export function moveSourceUi(ui, index, offset) {
  const destination = index + offset;
  if (index < 0 || index >= ui.ids.length || destination < 0 || destination >= ui.ids.length) return;
  const [id] = ui.ids.splice(index, 1);
  ui.ids.splice(destination, 0, id);
}

export function removeSourceUi(ui, index) {
  if (index < 0 || index >= ui.ids.length) return null;
  const [removedId] = ui.ids.splice(index, 1);
  ui.openIds.delete(removedId);
  ui.newIds.delete(removedId);
  const adjacentId = ui.ids[Math.min(index, ui.ids.length - 1)] || null;
  if (adjacentId) ui.openIds.add(adjacentId);
  return adjacentId;
}

export function markSourcesSaved(ui) {
  ui.newIds.clear();
}
