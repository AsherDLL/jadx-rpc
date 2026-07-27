package com.example.app;

import com.example.app.util.Hex;

public class Crypto {

    private static final String SECRET_KEY = "hunter2-not-a-real-key";

    private final String salt;

    public Crypto(String salt) {
        this.salt = salt;
    }

    public String encode(String input) {
        return Hex.toHex(mix(input, SECRET_KEY + this.salt));
    }

    public String decode(String input) {
        return mix(Hex.fromHex(input), SECRET_KEY + this.salt);
    }

    private static String mix(String data, String key) {
        StringBuilder out = new StringBuilder(data.length());
        for (int i = 0; i < data.length(); i++) {
            out.append((char) (data.charAt(i) ^ key.charAt(i % key.length())));
        }
        return out.toString();
    }
}
