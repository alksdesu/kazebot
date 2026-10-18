import { extensions } from './registry';

export type Command = (...args: any[]) => any;
export function executeCommand<T = unknown>(id: string, ...args: unknown[]): T {
  const command = extensions.resolve<Command>('commands', id);
  if (!command) throw new Error(`Command is unavailable: ${id}`);
  return command(...args) as T;
}
export function bindCommands(commands: Record<string, Command>) {
  for (const [id, handler] of Object.entries(commands)) extensions.builtin('commands', id, handler);
}
export function commandAction<T extends Command>(id: string, handler: T): T {
  extensions.builtin('commands', id, handler);
  return ((...args: unknown[]) => executeCommand(id, ...args)) as T;
}
