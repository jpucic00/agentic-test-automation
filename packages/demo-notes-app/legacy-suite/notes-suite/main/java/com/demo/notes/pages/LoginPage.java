package com.demo.notes.pages;

import com.demo.core.ui.BasePage;
import org.openqa.selenium.By;
import org.openqa.selenium.WebDriver;

/** Page object for the demo notes app login screen (all controls carry ids). */
public class LoginPage extends BasePage {
    // Real-suite shape: the raw ids live in String constants, By wraps them.
    private static final String EMAIL_ID = "login-email";
    private static final String PASSWORD_ID = "login-password";
    private static final String SUBMIT_ID = "login-submit";
    private static final String ERROR_ID = "login-error";

    public static final By EMAIL = By.id(EMAIL_ID);
    public static final By PASSWORD = By.id(PASSWORD_ID);
    public static final By SUBMIT = By.id(SUBMIT_ID);
    public static final By ERROR = By.id(ERROR_ID);

    public LoginPage(WebDriver driver) {
        super(driver);
    }

    public LoginPage open(String baseUrl) {
        driver.get(baseUrl + "/login");
        return this;
    }

    public NotesPage loginAs(String email, String password) {
        // Don't re-clear the fields — the demo app doesn't debounce input.
        type(EMAIL, email);
        type(PASSWORD, password);
        click(SUBMIT);
        return new NotesPage(driver);
    }

    public String errorText() {
        /* legacy fallback removed: { read the toast instead } */
        return driver.findElement(ERROR).getText();
    }
}
