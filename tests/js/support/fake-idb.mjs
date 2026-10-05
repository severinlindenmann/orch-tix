// A small IndexedDB stand-in for Node with the two properties bridge-store.js relies on: transactions on one store run
// one after another (as readwrite transactions do in every tab of an origin), and a transaction commits only after its
// last request's callback ran without starting another, or not at all if it aborted. Requests answer on a later
// macrotask, so callers that are not inside one transaction really do interleave.
const later = (f) => setTimeout(f, Math.random() < 0.5 ? 0 : 1);

export function fakeIndexedDB({ failOpen = false } = {}) {
  const dbs = new Map();
  return {
    dbs,
    open(name, version) {
      const req = {};
      later(() => {
        if (failOpen) { req.error = new Error("open failed"); req.onerror?.(); return; }
        let db = dbs.get(name);
        const fresh = !db;
        if (fresh) { db = { version, stores: new Map(), queues: new Map() }; dbs.set(name, db); }
        req.result = connection(db);
        if (fresh) req.onupgradeneeded?.();
        req.onsuccess?.();
      });
      return req;
    },
  };
}

function connection(db) {
  return {
    createObjectStore(name, opts = {}) { db.stores.set(name, { keyPath: opts.keyPath, data: new Map() }); },
    close() {},
    transaction(name, mode) {
      const store = db.stores.get(name);
      const queue = [];
      let work;                                    // the store as it is when this transaction starts
      let aborted = false, started = false;
      const t = {
        abort() { aborted = true; },
        objectStore() {
          const req = (op) => { const r = {}; queue.push(() => { r.result = op(); r.onsuccess?.(); }); if (started) pump(); return r; };
          return {
            get: (k) => req(() => work.get(k)),
            getAll: () => req(() => [...work.values()]),
            put: (v, k) => {
              if (mode !== "readwrite") throw new Error("ReadOnlyError");
              return req(() => { work.set(store.keyPath ? v[store.keyPath] : k, v); });
            },
            delete: (k) => req(() => { work.delete(k); }),
          };
        },
      };
      let busy = false;
      function pump() {
        if (busy) return;
        busy = true;
        later(function next() {
          if (aborted) { busy = false; finish(); return; }
          const op = queue.shift();
          if (!op) { busy = false; finish(); return; }
          op();
          later(next);
        });
      }
      let done;
      const turn = new Promise((r) => { done = r; });
      function finish() {
        if (aborted) { t.onabort?.(); done(); return; }
        if (queue.length) { pump(); return; }
        store.data = work;
        t.oncomplete?.();
        done();
      }
      const prev = db.queues.get(name) || Promise.resolve();
      db.queues.set(name, prev.then(() => { work = new Map(store.data); started = true; pump(); return turn; }));
      return t;
    },
  };
}
