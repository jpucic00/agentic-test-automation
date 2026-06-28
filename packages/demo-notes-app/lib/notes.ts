// Per-user notes, persisted in localStorage under "demo.notes.<email>". CRUD only.

export type Note = { id: string; title: string; body: string; updatedAt: number };

function notesKey(email: string): string {
  return `demo.notes.${email.toLowerCase()}`;
}

function hasStorage(): boolean {
  return typeof window !== "undefined" && !!window.localStorage;
}

function read(email: string): Note[] {
  if (!hasStorage()) return [];
  try {
    const raw = window.localStorage.getItem(notesKey(email));
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function write(email: string, notes: Note[]): void {
  if (!hasStorage()) return;
  window.localStorage.setItem(notesKey(email), JSON.stringify(notes));
}

function newId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.floor(Math.random() * 1_000_000)}`;
}

/** Notes for a user, newest first. */
export function listNotes(email: string): Note[] {
  return read(email).sort((a, b) => b.updatedAt - a.updatedAt);
}

export function createNote(email: string, title: string, body: string): Note {
  const note: Note = {
    id: newId(),
    title: title.trim(),
    body: body.trim(),
    updatedAt: Date.now(),
  };
  write(email, [note, ...read(email)]);
  return note;
}

export function updateNote(email: string, id: string, title: string, body: string): void {
  const next = read(email).map((n) =>
    n.id === id ? { ...n, title: title.trim(), body: body.trim(), updatedAt: Date.now() } : n,
  );
  write(email, next);
}

export function deleteNote(email: string, id: string): void {
  write(
    email,
    read(email).filter((n) => n.id !== id),
  );
}
