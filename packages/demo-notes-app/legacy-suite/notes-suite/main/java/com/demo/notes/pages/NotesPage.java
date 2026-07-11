package com.demo.notes.pages;

import com.demo.core.ui.BasePage;
import com.demo.core.ui.Locators;
import com.demo.core.util.Waits;
import org.openqa.selenium.By;
import org.openqa.selenium.WebDriver;

/**
 * Page object for the notes list. The editor controls are NON-SEMANTIC divs in
 * the app (no ids, no roles), so this page object descends the locator ladder:
 * name attributes where they exist, css/xpath where nothing better is offered.
 */
public class NotesPage extends BasePage {
    private static final String NOTE_TITLES_CSS = ".notes-list .note-item h3";

    // Row-scoped controls: note rows carry no ids, so the XPath is assembled at
    // runtime from the note's visible title — a template no static parser can fold.
    private static final String ROW_XPATH =
            "//li[contains(@class,'note-item')][.//h3[normalize-space()='%s']]";

    public static final By NEW_NOTE =
            By.xpath("//div[contains(@class,'btn') and normalize-space()='New note']");
    public static final By TITLE = By.name("title");
    public static final By BODY = By.name("body");
    public static final By SAVE =
            By.xpath("//div[contains(@class,'btn') and normalize-space()='Save note']");
    public static final By NOTE_TITLES = By.cssSelector(NOTE_TITLES_CSS);

    public NotesPage(WebDriver driver) {
        super(driver);
    }

    public static By noteRow(String title) {
        return By.xpath(String.format(ROW_XPATH, title));
    }

    public static By rowButton(String title, String label) {
        return By.xpath(
                String.format(ROW_XPATH, title)
                        + "//div[contains(@class,'btn') and normalize-space()='" + label + "']");
    }

    public void createNote(String title, String body) {
        click(NEW_NOTE);
        Waits.visible(driver, TITLE);
        type(TITLE, title);
        type(BODY, body);
        click(SAVE);
    }

    public void deleteNote(String title) {
        click(rowButton(title, "Delete"));
        // The dialog's confirm control resolves through the locator registry.
        Waits.visible(driver, Locators.byKey("notes.delete.confirm"));
        click(Locators.byKey("notes.delete.confirm"));
    }

    public boolean hasNote(String title) {
        return !driver.findElements(noteRow(title)).isEmpty();
    }

    public String firstNoteTitle() {
        Waits.visible(driver, NOTE_TITLES);
        return driver.findElement(NOTE_TITLES).getText();
    }
}
