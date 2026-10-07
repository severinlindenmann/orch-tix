// fileshare/static/js/bridge-wipe.js: sign-out's half of the bridge store (docs/bridge-protocol.md §2.6). Deleting the
// whole "fileshare-bridge" database takes the device key, every workspace key, the pinned host keys and the sequence
// counters with it. Its own tiny module so shell.js and outbox-ui.js (every page) do not load the crypto to do it.
const DB = "fileshare-bridge";

export function wipeBridge(factory = globalThis.indexedDB) {
  if (!factory) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const req = factory.deleteDatabase(DB);     // other tabs close their connection on versionchange (bridge-store.js)
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error || new Error("could not delete the bridge database"));
  });
}
