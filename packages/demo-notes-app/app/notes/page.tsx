"use client";

import { useRouter } from "next/navigation";
import { type SyntheticEvent, useEffect, useState } from "react";

import { currentUser } from "@/lib/auth";
import type { Note } from "@/lib/notes";
import { createNote, deleteNote, listNotes, updateNote } from "@/lib/notes";

export default function NotesPage() {
  const router = useRouter();
  const [email, setEmail] = useState<string | null>(null);
  const [notes, setNotes] = useState<Note[]>([]);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<Note | null>(null);

  useEffect(() => {
    const current = currentUser();
    if (!current) {
      router.replace("/login");
      return;
    }
    setEmail(current);
    setNotes(listNotes(current));
  }, [router]);

  function openCreate() {
    setEditingId(null);
    setTitle("");
    setBody("");
    setEditorOpen(true);
  }

  function openEdit(note: Note) {
    setEditingId(note.id);
    setTitle(note.title);
    setBody(note.body);
    setEditorOpen(true);
  }

  function closeEditor() {
    setEditorOpen(false);
    setEditingId(null);
    setTitle("");
    setBody("");
  }

  function handleSave(event: SyntheticEvent) {
    event.preventDefault();
    if (!email) return;
    if (editingId) {
      updateNote(email, editingId, title, body);
    } else {
      createNote(email, title, body);
    }
    closeEditor();
    setNotes(listNotes(email));
  }

  function confirmDelete() {
    if (!email || !deleteTarget) return;
    deleteNote(email, deleteTarget.id);
    setDeleteTarget(null);
    setNotes(listNotes(email));
  }

  if (!email) {
    return null; // redirecting to /login
  }

  return (
    <section>
      <div className="notes-header">
        <h1>Your notes</h1>
        {/* Non-semantic div controls (no button/role/id/aria) — resilience-ladder fixture. */}
        <div className="btn" onClick={openCreate}>
          New note
        </div>
      </div>

      {editorOpen ? (
        <form className="card note-editor" onSubmit={handleSave}>
          <label>
            Title
            <input
              name="title"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
            />
          </label>
          <label>
            Body
            <textarea
              name="body"
              rows={4}
              value={body}
              onChange={(event) => setBody(event.target.value)}
            />
          </label>
          <div className="actions">
            <div className="btn" onClick={handleSave}>
              Save note
            </div>
            <div className="btn" onClick={closeEditor}>
              Cancel
            </div>
          </div>
        </form>
      ) : null}

      {notes.length === 0 ? (
        <p className="empty">No notes yet. Click “New note” to add one.</p>
      ) : (
        // No per-row ids: a note is located by its visible title, then its sibling Edit/Delete
        // control (non-semantic divs) — resilience-ladder fixture.
        <ul className="notes-list">
          {notes.map((note) => (
            <li key={note.id} className="note-item">
              <div>
                <h3>{note.title || "(untitled)"}</h3>
                <p className="note-body">{note.body}</p>
              </div>
              <div className="actions">
                <div className="btn" onClick={() => openEdit(note)}>
                  Edit
                </div>
                <div className="btn" onClick={() => setDeleteTarget(note)}>
                  Delete
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}

      {deleteTarget ? (
        <div className="modal-overlay">
          {/* No role="dialog"/aria/id — scope to the .modal container, then by text. */}
          <div className="modal">
            <h2>Delete note</h2>
            <p>Delete “{deleteTarget.title || "(untitled)"}”? This cannot be undone.</p>
            <div className="actions">
              <div className="btn" onClick={confirmDelete}>
                Delete
              </div>
              <div className="btn" onClick={() => setDeleteTarget(null)}>
                Cancel
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}
