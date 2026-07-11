package com.demo.core.ui;

import java.io.IOException;
import java.io.InputStream;
import java.util.Properties;
import org.openqa.selenium.By;

/**
 * Central locator registry: keys resolve to values in /locators.properties on the
 * classpath. Values starting with "//" are XPath expressions; anything else is an
 * element id. Legacy pattern — the locator VALUES live in a resource file, so no
 * page-object source contains them.
 */
public final class Locators {
    private static final Properties REGISTRY = load();

    private Locators() {}

    private static Properties load() {
        Properties properties = new Properties();
        try (InputStream stream = Locators.class.getResourceAsStream("/locators.properties")) {
            if (stream == null) {
                throw new IllegalStateException("locators.properties not on the classpath");
            }
            properties.load(stream);
        } catch (IOException e) {
            throw new IllegalStateException("could not read locators.properties", e);
        }
        return properties;
    }

    /** The registered locator for {@code key}, e.g. {@code byKey("notes.delete.confirm")}. */
    public static By byKey(String key) {
        String value = REGISTRY.getProperty(key);
        if (value == null) {
            throw new IllegalArgumentException("no locator registered for key: " + key);
        }
        return value.startsWith("//") ? By.xpath(value) : By.id(value);
    }
}
