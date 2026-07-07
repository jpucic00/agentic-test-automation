package com.demo.notes.tests;

import static org.junit.jupiter.api.Assertions.assertTrue;

import com.demo.core.xray.Xray;
import com.demo.notes.pages.LoginPage;
import org.junit.jupiter.api.Test;
import org.openqa.selenium.WebDriver;

public class LoginTest {
    private WebDriver driver;
    private final String baseUrl = "http://localhost:3000";

    @Xray(testCase = "NOTE-4")
    @Test
    public void loginFailsWithWrongPassword() {
        LoginPage login = new LoginPage(driver);
        login.open(baseUrl);
        login.loginAs("demo@demo.test", "not-the-password");
        assertTrue(login.errorText().toLowerCase().contains("invalid"));
        // Result upload lives in a shared reporting jar, not in this repo.
        ReportingClient.record("NOTE-4", true);
    }
}
