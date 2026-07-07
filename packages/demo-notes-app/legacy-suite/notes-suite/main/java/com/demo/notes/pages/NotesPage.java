package com.demo.notes.pages;

import com.demo.core.ui.BasePage;
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

    public void createNote(String title, String body) {
        click(NEW_NOTE);
        Waits.visible(driver, TITLE);
        type(TITLE, title);
        type(BODY, body);
        click(SAVE);
    }

    public String firstNoteTitle() {
        Waits.visible(driver, NOTE_TITLES);
        return driver.findElement(NOTE_TITLES).getText();
    }
}
