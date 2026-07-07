package com.demo.notes.pages;

import org.openqa.selenium.By;
import org.openqa.selenium.WebDriver;

/** Page object for the demo notes app login screen (all controls carry ids). */
public class LoginPage {
    public static final By EMAIL = By.id("login-email");
    public static final By PASSWORD = By.id("login-password");
    public static final By SUBMIT = By.id("login-submit");
    public static final By ERROR = By.id("login-error");

    private final WebDriver driver;

    public LoginPage(WebDriver driver) {
        this.driver = driver;
    }

    public void open(String baseUrl) {
        driver.get(baseUrl + "/login");
    }

    public void loginAs(String email, String password) {
        driver.findElement(EMAIL).clear();
        driver.findElement(EMAIL).sendKeys(email);
        driver.findElement(PASSWORD).sendKeys(password);
        driver.findElement(SUBMIT).click();
    }

    public String errorText() {
        return driver.findElement(ERROR).getText();
    }
}
