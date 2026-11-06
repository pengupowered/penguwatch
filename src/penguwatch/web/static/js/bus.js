// A small publish/subscribe channel between the shell, the tabs and the dialogs: a module tells what happened
// ("show-updated", "services-loaded", ...) and whoever shows that data refreshes it, so no module imports another
// tab's module. emit() resolves when every handler has (a handler's error is logged, never thrown at the sender).
const topics = new Map();

export function on(topic, fn) {
  if (!topics.has(topic)) topics.set(topic, []);
  topics.get(topic).push(fn);
}

export function emit(topic, value) {
  return Promise.all((topics.get(topic) || []).map(async (fn) => {
    try {
      return await fn(value);
    } catch (err) {
      console.error(`${topic} handler failed:`, err);
      return undefined;
    }
  }));
}
