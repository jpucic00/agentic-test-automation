package com.demo.core.ui;

import com.demo.core.util.Waits;
import org.openqa.selenium.By;
import org.openqa.selenium.WebDriver;

/** Shared page-object base — every suite's pages extend this. */
public abstract class BasePage {
    protected final WebDriver driver;

    protected BasePage(WebDriver driver) {
        this.driver = driver;
    }

    protected void click(By locator) {
        Waits.visible(driver, locator);
        driver.findElement(locator).click();
    }

    protected void type(By locator, String text) {
        Waits.visible(driver, locator);
        driver.findElement(locator).clear();
        driver.findElement(locator).sendKeys(text);
    }
}
