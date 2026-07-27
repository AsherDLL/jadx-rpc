package com.example.app.util;

public final class Hex {

    private static final char[] DIGITS = "0123456789abcdef".toCharArray();

    private Hex() {
    }

    public static String toHex(String input) {
        StringBuilder out = new StringBuilder(input.length() * 2);
        for (int i = 0; i < input.length(); i++) {
            char c = input.charAt(i);
            out.append(DIGITS[(c >> 4) & 0xf]).append(DIGITS[c & 0xf]);
        }
        return out.toString();
    }

    public static String fromHex(String input) {
        StringBuilder out = new StringBuilder(input.length() / 2);
        for (int i = 0; i + 1 < input.length(); i += 2) {
            out.append((char) Integer.parseInt(input.substring(i, i + 2), 16));
        }
        return out.toString();
    }
}
