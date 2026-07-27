package net.thirdparty.lib;

// Stands in for a bundled third-party library: a different vendor prefix from
// the fixture's own com.example code, so scope filtering has something to hide.
public final class Helper {

    private static final String VENDOR_BANNER = "thirdparty-vendor-banner";

    private Helper() {
    }

    public static String describe(String input) {
        return VENDOR_BANNER + ":" + input.length();
    }
}
