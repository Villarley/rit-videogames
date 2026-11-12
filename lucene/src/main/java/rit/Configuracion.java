package rit;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Lee el mismo .conf que el indexador propio (formato INI de Linux).
 * Secciones entre corchetes, "clave = valor", comentarios con # o ;.
 * Las rutas relativas se resuelven desde la raíz del repo (la carpeta padre de lucene/).
 */
public final class Configuracion {
    private final Map<String, Map<String, String>> secciones = new LinkedHashMap<>();
    public final Path archivo;
    public final Path raizRepo;

    public Configuracion(Path archivo, Path raizRepo) throws IOException {
        this.archivo = archivo;
        this.raizRepo = raizRepo;
        String actual = "";
        for (String linea : Files.readAllLines(archivo, StandardCharsets.UTF_8)) {
            linea = quitarComentario(linea).strip();
            if (linea.isEmpty()) continue;
            if (linea.startsWith("[") && linea.endsWith("]")) {
                actual = linea.substring(1, linea.length() - 1).strip();
                secciones.computeIfAbsent(actual, k -> new LinkedHashMap<>());
                continue;
            }
            int igual = linea.indexOf('=');
            if (igual < 0) throw new IOException("línea inválida en " + archivo + ": " + linea);
            secciones.computeIfAbsent(actual, k -> new LinkedHashMap<>())
                    .put(linea.substring(0, igual).strip().toLowerCase(), linea.substring(igual + 1).strip());
        }
    }

    private static String quitarComentario(String linea) {
        int a = linea.indexOf('#');
        int b = linea.indexOf(';');
        int corte = a < 0 ? b : (b < 0 ? a : Math.min(a, b));
        return corte < 0 ? linea : linea.substring(0, corte);
    }

    public String texto(String seccion, String clave, String porDefecto) {
        Map<String, String> s = secciones.get(seccion);
        if (s == null) return porDefecto;
        return s.getOrDefault(clave, porDefecto);
    }

    public int entero(String seccion, String clave, int porDefecto) {
        String v = texto(seccion, clave, null);
        return v == null || v.isEmpty() ? porDefecto : Integer.parseInt(v);
    }

    public static boolean esNo(String v) {
        if (v == null) return true;
        String x = v.strip().toLowerCase();
        return x.isEmpty() || x.equals("no") || x.equals("false") || x.equals("0") || x.equals("off");
    }

    public boolean booleano(String seccion, String clave, boolean porDefecto) {
        String v = texto(seccion, clave, null);
        if (v == null) return porDefecto;
        return !esNo(v);
    }

    /** null si el valor es "no". */
    public Path ruta(String seccion, String clave, String porDefecto) {
        String v = texto(seccion, clave, porDefecto);
        if (esNo(v)) return null;
        if (v.startsWith("~/")) v = System.getProperty("user.home") + v.substring(1);
        Path p = Path.of(v);
        return p.isAbsolute() ? p : raizRepo.resolve(p).normalize();
    }
}
