package com.example.app;

public class Main {

    static class Result {
        final String value;

        Result(String value) {
            this.value = value;
        }
    }

    public static void main(String[] args) {
        Crypto crypto = new Crypto("pepper");
        Result r = run(crypto, "https://example.invalid/api/v1/report");
        System.out.println(r.value);
    }

    static Result run(Crypto crypto, String payload) {
        String encoded = crypto.encode(payload);
        return new Result(crypto.decode(encoded));
    }
}
