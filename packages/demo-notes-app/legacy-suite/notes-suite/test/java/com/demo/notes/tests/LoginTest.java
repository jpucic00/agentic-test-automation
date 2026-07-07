package com.demo.notes.tests;

import static com.demo.core.util.Waits.visible;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.demo.core.xray.Xray;
import com.demo.notes.pages.LoginPage;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.openqa.selenium.WebDriver;

public class LoginTest {
    private WebDriver driver;
    private LoginPage loginPage;
    private final String baseUrl = "http://localhost:3000";

    @BeforeEach
    public void setUp() {
        loginPage = new LoginPage(driver);
    }

    @Xray(testCase = "NOTE-4")
    @Test
    public void loginFailsWithWrongPassword() {
        loginPage.open(baseUrl);
        loginPage.loginAs("demo@demo.test", "not-the-password");
        visible(driver, LoginPage.ERROR);
        this.verifyStillOnLogin();
        assertTrue(loginPage.errorText().toLowerCase().contains("invalid"));
        // Result upload lives in a shared reporting jar, not in this repo.
        ReportingClient.record("NOTE-4", true);
    }

    private void verifyStillOnLogin() {
        assertTrue(driver.getCurrentUrl().contains("/login"));
    }
}
