package rit;

import java.io.IOException;
import java.io.Reader;
import java.util.HashMap;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.apache.lucene.analysis.CharFilter;

/**
 * Fechas y números, antes de tokenizar. Es la misma lógica que
 * normalizar_fechas y normalizar_numeros de indexer/normalizacion.py, con los
 * mismos patrones escritos para que signifiquen lo mismo en Java y en Python
 * (UNICODE_CHARACTER_CLASS hace que \b y \s se comporten como en Python).
 *
 * Lucene trae PatternReplaceCharFilter, pero no sirve para las fechas: el
 * reemplazo tiene que convertir "October" en 10 y rellenar el día con cero, y
 * eso no se puede con una cadena de reemplazo. Por eso este CharFilter lee el
 * documento entero (como hace PatternReplaceCharFilter por dentro) y aplica
 * los reemplazos con una función.
 */
public final class NormalizadorCharFilter extends CharFilter {

    static final Map<String, Integer> MESES = new HashMap<>();

    static {
        Object[][] m = {
            {"january", 1}, {"jan", 1}, {"february", 2}, {"feb", 2}, {"march", 3}, {"mar", 3},
            {"april", 4}, {"apr", 4}, {"may", 5}, {"june", 6}, {"jun", 6}, {"july", 7}, {"jul", 7},
            {"august", 8}, {"aug", 8}, {"september", 9}, {"sept", 9}, {"sep", 9},
            {"october", 10}, {"oct", 10}, {"november", 11}, {"nov", 11}, {"december", 12}, {"dec", 12},
            {"enero", 1}, {"ene", 1}, {"febrero", 2}, {"marzo", 3}, {"abril", 4}, {"abr", 4},
            {"mayo", 5}, {"junio", 6}, {"julio", 7}, {"agosto", 8}, {"ago", 8},
            {"septiembre", 9}, {"setiembre", 9}, {"set", 9}, {"octubre", 10}, {"noviembre", 11},
            {"diciembre", 12}, {"dic", 12},
        };
        for (Object[] par : m) MESES.put((String) par[0], (Integer) par[1]);
    }

    private static final String MES_ALT;

    static {
        // Igual que _MES en Python: más largos primero y luego alfabético.
        MES_ALT = String.join("|", MESES.keySet().stream()
                .sorted((a, b) -> a.length() != b.length() ? b.length() - a.length() : a.compareTo(b))
                .toList());
    }

    private static final int U = Pattern.UNICODE_CHARACTER_CLASS;
    static final Pattern ISO = Pattern.compile("\\b([0-9]{4})[-/.]([0-9]{1,2})[-/.]([0-9]{1,2})\\b", U);
    static final Pattern MES_DIA = Pattern.compile(
            "\\b([A-Za-z]{3,10})\\.?\\s+([0-9]{1,2})(?:st|nd|rd|th)?,?\\s+([0-9]{4})\\b", U);
    static final Pattern DIA_MES = Pattern.compile(
            "\\b([0-9]{1,2})(?:st|nd|rd|th)?\\s+(?:de\\s+)?(" + MES_ALT + ")\\.?,?\\s+(?:del?\\s+)?([0-9]{4})\\b",
            U | Pattern.CASE_INSENSITIVE | Pattern.UNICODE_CASE);
    static final Pattern NUMERICA = Pattern.compile("\\b([0-9]{1,2})[/.-]([0-9]{1,2})[/.-]([0-9]{4})\\b", U);
    static final Pattern MILES = Pattern.compile("(?<=[0-9]),(?=[0-9]{3}(?![0-9]))", U);
    static final Pattern ORDINAL = Pattern.compile("\\b([0-9]+)(?:st|nd|rd|th)\\b",
            U | Pattern.CASE_INSENSITIVE | Pattern.UNICODE_CASE);

    private final boolean fechas;
    private final boolean numeros;
    private String salida;
    private int pos;

    public NormalizadorCharFilter(Reader entrada, boolean fechas, boolean numeros) {
        super(entrada);
        this.fechas = fechas;
        this.numeros = numeros;
    }

    static String fecha(int anio, int mes, int dia) {
        if (mes >= 1 && mes <= 12 && dia >= 1 && dia <= 31 && anio >= 1000 && anio <= 2999) {
            return String.format("%04d%02d%02d", anio, mes, dia);
        }
        return null;
    }

    private static String o(String r, java.util.regex.MatchResult m) {
        return r != null ? r : m.group();
    }

    public static String normalizarFechas(String t) {
        t = ISO.matcher(t).replaceAll(m -> Matcher.quoteReplacement(o(
                fecha(Integer.parseInt(m.group(1)), Integer.parseInt(m.group(2)), Integer.parseInt(m.group(3))), m)));
        t = MES_DIA.matcher(t).replaceAll(m -> {
            Integer mes = MESES.get(m.group(1).toLowerCase());
            if (mes == null) return Matcher.quoteReplacement(m.group());
            return Matcher.quoteReplacement(o(fecha(Integer.parseInt(m.group(3)), mes, Integer.parseInt(m.group(2))), m));
        });
        t = DIA_MES.matcher(t).replaceAll(m -> Matcher.quoteReplacement(o(fecha(
                Integer.parseInt(m.group(3)), MESES.get(m.group(2).toLowerCase()), Integer.parseInt(m.group(1))), m)));
        t = NUMERICA.matcher(t).replaceAll(m -> {
            int a = Integer.parseInt(m.group(1));
            int b = Integer.parseInt(m.group(2));
            int anio = Integer.parseInt(m.group(3));
            // Igual que en Python: si el primero pasa de 12 es día/mes; si no, mes/día.
            String r = a > 12 ? fecha(anio, b, a) : fecha(anio, a, b);
            return Matcher.quoteReplacement(o(r, m));
        });
        return t;
    }

    public static String normalizarNumeros(String t) {
        t = MILES.matcher(t).replaceAll("");
        return ORDINAL.matcher(t).replaceAll("$1");
    }

    private void cargar() throws IOException {
        StringBuilder sb = new StringBuilder();
        char[] buf = new char[8192];
        int n;
        while ((n = input.read(buf)) > 0) sb.append(buf, 0, n);
        String t = sb.toString();
        if (fechas) t = normalizarFechas(t);
        if (numeros) t = normalizarNumeros(t);
        salida = t;
        pos = 0;
    }

    @Override
    public int read(char[] cbuf, int off, int len) throws IOException {
        if (salida == null) cargar();
        if (pos >= salida.length()) return -1;
        int n = Math.min(len, salida.length() - pos);
        salida.getChars(pos, pos + n, cbuf, off);
        pos += n;
        return n;
    }

    @Override
    protected int correct(int currentOff) {
        // No se guardan offsets en el índice, así que no hace falta corregirlos.
        return currentOff;
    }
}
