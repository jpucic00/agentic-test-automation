package com.demo.notes.tests;

import static org.junit.jupiter.api.Assertions.assertFalse;

import com.demo.core.xray.Xray;
import com.demo.notes.flows.NoteFlows;
import com.demo.notes.pages.NotesPage;
import org.junit.jupiter.api.Test;
import org.openqa.selenium.WebDriver;

public class DeleteNoteTest {
    private WebDriver driver;
    private final String baseUrl = "http://localhost:3000";

    @Xray(testCase = "NOTE-5")
    @Test
    public void seededUserDeletesANote() {
        NoteFlows flows = new NoteFlows(driver);
        NotesPage notes = flows.openNotesAsSeededUser(baseUrl);
        flows.createThenDelete(notes, "Disposable", "Delete me");
        assertFalse(notes.hasNote("Disposable"));
    }
}
