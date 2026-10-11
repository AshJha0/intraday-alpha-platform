package com.iap.portfolio;

import java.util.ArrayList;
import java.util.List;
import java.util.TreeMap;
import java.util.TreeSet;

/**
 * Currency-exposure translation for FX pair portfolios
 * (API_PORTFOLIO_TCA.md §1.2): for a pair column {@code BASE/QUOTE} the
 * exposure matrix E carries +1 in the BASE row and -1 in the QUOTE row;
 * currency row order is <b>sorted alphabetically</b> (pinned). Net currency
 * exposure of the book is {@code E w}.
 */
public final class CurrencyExposure {
    /** The sorted currency list and the (currencies x pairs) matrix. */
    public record Result(List<String> currencies, double[][] matrix) {
    }

    private CurrencyExposure() {
    }

    /** Build (currencies, E) for pair symbols like {@code "EUR/USD"}. */
    public static Result matrix(List<String> pairSymbols) {
        List<String[]> parsed = new ArrayList<>();
        TreeSet<String> ccys = new TreeSet<>();
        for (String sym : pairSymbols) {
            String[] parts = sym.split("/", -1);
            if (parts.length != 2 || parts[0].isEmpty() || parts[1].isEmpty()) {
                throw new IllegalArgumentException(
                        "not a currency pair symbol: " + sym);
            }
            if (parts[0].equals(parts[1])) {
                throw new IllegalArgumentException("degenerate pair: " + sym);
            }
            parsed.add(parts);
            ccys.add(parts[0]);
            ccys.add(parts[1]);
        }
        List<String> currencies = new ArrayList<>(ccys);
        TreeMap<String, Integer> row = new TreeMap<>();
        for (int i = 0; i < currencies.size(); i++) {
            row.put(currencies.get(i), i);
        }
        double[][] e = new double[currencies.size()][parsed.size()];
        for (int j = 0; j < parsed.size(); j++) {
            e[row.get(parsed.get(j)[0])][j] = 1.0;
            e[row.get(parsed.get(j)[1])][j] = -1.0;
        }
        return new Result(currencies, e);
    }
}
