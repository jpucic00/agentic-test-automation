package com.demo.notes.tests;

import static org.junit.jupiter.api.Assertions.assertEquals;

import com.demo.core.xray.Xray;
import com.demo.notes.pages.LoginPage;
import com.demo.notes.pages.NotesPage;
import org.junit.jupiter.api.Test;
import org.openqa.selenium.WebDriver;

public class CreateNoteTest {
    private WebDriver driver;
    private final String baseUrl = "http://localhost:3000";

    @Xray(testCase = "NOTE-2")
    @Test
    public void seededUserCreatesANote() {
        NotesPage notes =
                new LoginPage(driver).open(baseUrl).loginAs("demo@demo.test", "Passw0rd!");
        notes.createNote("Groceries", "Milk, eggs, coffee");
        assertEquals("Groceries", notes.firstNoteTitle());
    }
}
