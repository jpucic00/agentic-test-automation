package com.demo.notes.flows;

import com.demo.notes.pages.LoginPage;
import com.demo.notes.pages.NotesPage;
import org.openqa.selenium.WebDriver;

/**
 * Cross-page flows — the layer tests call instead of raw page objects, so one
 * test line hides several page interactions (test → flow → page → base/registry).
 */
public final class NoteFlows {
    private final WebDriver driver;

    public NoteFlows(WebDriver driver) {
        this.driver = driver;
    }

    /** Log in as the seeded demo user and land on the notes list. */
    public NotesPage openNotesAsSeededUser(String baseUrl) {
        return new LoginPage(driver).open(baseUrl).loginAs("demo@demo.test", "Passw0rd!");
    }

    /** Create a note, then remove it again via the row control + confirm dialog. */
    public void createThenDelete(NotesPage notes, String title, String body) {
        notes.createNote(title, body);
        notes.deleteNote(title);
    }
}
