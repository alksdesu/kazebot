import { reportPluginError } from './status';

type Handler = (input: string, init: RequestInit) => Promise<Response>;
const attributed = new WeakSet<object>();

export function guardRequest(handler: Handler, owner: string, generation: string): Handler {
  return async (input, init) => {
    try { return await handler(input, init); }
    catch (error) {
      const reference = error && (typeof error === 'object' || typeof error === 'function') ? error as object : null;
      if (!reference || !attributed.has(reference)) {
        if (reference) attributed.add(reference);
        reportPluginError(owner, generation, error);
      }
      throw error;
    }
  };
}
